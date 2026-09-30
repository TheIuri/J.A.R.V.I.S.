"""Pipeline: audio -> STT -> LLM (+ tools) -> TTS, con latencia medida por etapa.

Cada paso se puede seguir en directo con `on_event` (el "flujo de pensamiento" del HUD):
  heard -> memory -> thinking -> tool / tool_result (por cada llamada) -> reply -> speaking
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field

from typing import Any, Callable

from .llm import FallbackLLM
from .memory import Retriever, as_prompt
from .memory.retrieval import keywords
from .shortcuts import direct_answer, select_specs
from .stt import STT
from .tools import ToolContext, ToolRegistry
from .tools.registry import PendingAction
from .tts import TTS

log = logging.getLogger(__name__)

# Rondas maximas de "el modelo pide tool -> se ejecuta -> vuelve al modelo" por turno.
MAX_TOOL_ROUNDS = 4
# Lo que se ensena en el HUD de cada resultado de tool (el LLM recibe el resultado completo).
EVENT_TEXT_CHARS = 160

EventSink = Callable[[dict[str, Any]], None]

# Acciones que piden confirmacion: el "si" tiene que llegar en el turno siguiente y antes de este plazo.
DIRECT = "directo:0 tokens"  # "proveedor" de las respuestas sin LLM (el HUD lo enseña como Directo · 0 tokens)
PENDING_TTL_S = 300  # con Claude y la voz, 2 minutos se quedaban cortos
_YES_START = set("si vale ok okay confirmo confirmado adelante dale claro venga correcto afirmativo perfecto genial "
                 "exacto eso porfa".split())
# "si, crealo", "apuntalo", "si, ponlo en el calendario": verbos de confirmar la accion propuesta.
_YES_VERBS = set("hazlo crealo apuntalo anotalo ponlo guardalo confirmalo agendalo reinicialo enciendelo mandalo "
                 "envialo haz crea apunta anota pon guarda confirma agenda sigue continua procede".split())
_YES_FILL = set("por favor gracias jarvis lo la el ya asi tal como esta bien muy todo supuesto en calendario evento "
                "cita recordatorio de acuerdo me parece sin problema".split())
_NO = set("no pero espera cambia cambialo mejor otro otra otra antes cancela cancelalo para nunca tampoco".split())


def _plain_words(text: str) -> list[str]:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return re.sub(r"[^a-z ]", " ", plain).split()


def is_affirmative(text: str) -> bool:
    """Un "si" corto e inequivoco ("si", "vale", "si, crealo", "apuntalo por favor"). Si hay un "no", un "pero" o
    algo que cambiar, no lo es."""
    words = _plain_words(text)
    if not words or len(words) > 10 or any(w in _NO for w in words):
        return False
    if words[0] not in _YES_START and words[0] not in _YES_VERBS:
        return False
    return all(w in _YES_START or w in _YES_VERBS or w in _YES_FILL for w in words)


def is_negative(text: str) -> bool:
    words = _plain_words(text)
    return bool(words) and words[0] in _NO


def _no_events(event: dict[str, Any]) -> None:
    pass


def _short(text: str, limit: int = EVENT_TEXT_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _args(raw: str) -> dict[str, Any]:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return {k: _short(v, 60) if isinstance(v, str) else v for k, v in args.items()} if isinstance(args, dict) else {}


@dataclass
class TurnResult:
    transcript: str
    reply: str
    provider: str | None
    audio: bytes | None
    timings_ms: dict[str, int] = field(default_factory=dict)
    tools_used: list[str] = field(default_factory=list)
    pc_actions: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def _timed(timings: dict[str, int], stage: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = round((time.perf_counter() - start) * 1000)


class Assistant:
    def __init__(
        self,
        stt: STT | None,
        llm: FallbackLLM,
        tts: TTS,
        system_prompt: str,
        history_turns: int = 6,
        tools: ToolRegistry | None = None,
        memory: Retriever | None = None,
    ):
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.tools = tools
        self.memory = memory
        self.system_prompt = system_prompt
        # Contexto de la conversacion en curso, solo en RAM (la memoria persistente es el Nivel 3).
        self._history: dict[str, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=history_turns * 2))
        self._history_turns = history_turns
        self._seeded: set[str] = set()  # sesiones ya retomadas del registro (o empezadas de cero a proposito)
        # Ahorro de tokens (shortcuts.py): solo las herramientas que vienen a cuento y lo trivial sin LLM.
        self.tool_filter = True
        self.direct_answers = True
        self._last_groups: dict[str, set[str]] = {}
        # Informes de agentes que han terminado y la conversacion aun no ha visto (se pasan en el siguiente turno).
        self._news: list[str] = []
        self._news_lock = threading.Lock()
        self._pending: dict[str, tuple[PendingAction, float]] = {}  # accion esperando un "si", por sesion
        self.board = None  # tablon de avisos proactivos (notify.NoticeBoard), si esta activo
        self.vault = None  # boveda de Obsidian, si esta configurada
        self.turn_log = None  # turnlog.TurnLog: conversaciones del dia para el resumen nocturno
        self.activity = None  # activity.ActivityLog: trazabilidad de agentes para el HUD
        self.team = None  # agents.AgentTeam
        self.insights = None  # insights.Insights: fichas con los datos clave de cada respuesta (HUD)
        self.watcher = None
        # Un turno cada vez: evita pelearse por la GPU y mantiene el orden del historial.
        self._lock = threading.Lock()

    def handle_audio(
        self,
        audio: bytes,
        session: str = "default",
        speak: bool = True,
        pc_apps: list[str] | None = None,
        on_event: EventSink | None = None,
        model: str | None = None,
        image: str | None = None,
    ) -> TurnResult:
        if self.stt is None:
            raise RuntimeError("No hay proveedor STT configurado")
        emit = on_event or _no_events
        with self._lock:
            timings: dict[str, int] = {}
            emit({"type": "listening"})
            with _timed(timings, "stt"):
                transcript = self.stt.transcribe(audio)
            log.info("[%s] STT %dms: %r", session, timings["stt"], transcript)
            emit({"type": "heard", "text": transcript, "ms": timings["stt"]})
            if not transcript:
                return TurnResult("", "", None, None, timings)
            ctx = ToolContext(pc_apps=pc_apps, image=image)
            return self._respond(transcript, session, speak, timings, ctx, emit, model)

    def handle_text(
        self,
        text: str,
        session: str = "default",
        speak: bool = True,
        pc_apps: list[str] | None = None,
        on_event: EventSink | None = None,
        model: str | None = None,
        image: str | None = None,
    ) -> TurnResult:
        emit = on_event or _no_events
        with self._lock:
            text = text.strip()
            emit({"type": "heard", "text": text, "ms": 0})
            return self._respond(text, session, speak, {}, ToolContext(pc_apps=pc_apps, image=image), emit, model)

    def transcribe(self, audio: bytes) -> str:
        """Solo STT (modo Claude: el PC transcribe aqui y piensa con Claude Code)."""
        if self.stt is None:
            raise RuntimeError("No hay proveedor STT configurado")
        with self._lock:
            return self.stt.transcribe(audio)

    def speak(self, text: str) -> bytes | None:
        """Solo TTS (p. ej. el aviso de un temporizador del PC)."""
        with self._lock:
            return self.tts.synthesize(text)

    def reset(self, session: str = "default") -> None:
        self._history.pop(session, None)
        self._pending.pop(session, None)
        self._seeded.add(session)  # "Nueva conversacion": no se retoma la anterior del registro

    def _resume_history(self, session: str) -> None:
        """Tras reiniciar o actualizar JARVIS, la conversacion sigue donde estaba (desde el registro en disco)."""
        if session in self._seeded or not self.turn_log:
            return
        self._seeded.add(session)
        if self._history.get(session):
            return
        history = self._history[session]
        for user, reply in self.turn_log.recent(session, self._history_turns):
            history.append({"role": "user", "content": user})
            history.append({"role": "assistant", "content": reply})

    def note_agent_done(self, label: str, job) -> None:
        """Un agente ha terminado: su informe (tablas, enlaces...) pasa al siguiente turno de la conversacion."""
        from .agents import NEWS_REPORT_CHARS

        where = f" (guardado en Obsidian: {job.note})" if job.note else ""
        with self._news_lock:
            self._news.append(f"{label} ha terminado el encargo «{job.topic}»{where}. Su informe:\n"
                              f"{job.report[:NEWS_REPORT_CHARS]}")
            del self._news[:-3]

    def take_news(self) -> str:
        with self._news_lock:
            news, self._news = self._news, []
        if not news:
            return ""
        return ("Novedades desde el ultimo mensaje (ya estan terminadas: no digas que sigues esperando). Si el usuario "
                "pide datos, enlaces o una tabla de esto, sacalos de aqui:\n\n" + "\n\n".join(news))

    def direct(self, text: str, session: str, speak: bool, emit: EventSink) -> TurnResult | None:
        """Respuesta directa sin LLM (modo Claude: se intenta antes de despertar a Claude)."""
        if not self.direct_answers or session in self._pending:
            return None
        with self._lock:
            return self._direct(text.strip(), session, speak, {}, ToolContext(), emit)

    def _direct(self, text: str, session: str, speak: bool, timings: dict[str, int], ctx: ToolContext,
                emit: EventSink) -> TurnResult | None:
        with _timed(timings, "tool"):
            hit = direct_answer(text, self.tools, ctx)
        if not hit:
            return None
        emit({"type": "tool", "id": "direct", "name": hit.tool, "args": {}})
        emit({"type": "tool_result", "id": "direct", "name": hit.tool, "ok": not hit.reply.startswith("No he podido"),
              "ms": timings["tool"], "text": _short(hit.reply)})
        if ctx.cards:
            emit({"type": "cards", "cards": ctx.cards})
        emit({"type": "reply", "text": hit.reply, "provider": DIRECT, "ms": 0})
        log.info("[%s] respuesta directa (0 tokens) con %s: %r", session, hit.tool, hit.reply)
        self._resume_history(session)
        history = self._history[session]
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": hit.reply})
        if self.turn_log:
            self.turn_log.add(session, text, hit.reply)
        audio = None
        if speak:
            emit({"type": "speaking"})
            with _timed(timings, "tts"):
                audio = self.tts.synthesize(hit.reply)
        timings["total"] = sum(timings.values())
        return TurnResult(text, hit.reply, DIRECT, audio, timings, [hit.tool], ctx.pc_actions, ctx.cards)

    def claude_context(self, session: str, text: str, fresh: bool = False) -> str:
        """Para una conversacion nueva de Claude (la primera, tras reiniciar o al compactar una larga): lo ultimo
        que hablasteis en esta sesion y las conversaciones anteriores relacionadas con lo que acaba de decir."""
        if not self.turn_log:
            return ""
        if fresh:  # compactada: resumen sin LLM de lo reciente, un poco mas largo
            recent = self.turn_log.recent(session, self._history_turns + 4, hours=24)
        else:
            recent = [] if session in self._seeded else self.turn_log.recent(session, self._history_turns)
        self._seeded.add(session)
        out = ""
        if recent:
            why = "la conversacion se ha compactado para gastar menos" if fresh else "antes de reiniciarte"
            lines = "\n".join(f"- Usuario: {u[:300]} -> Tu: {r[:400]}" for u, r in recent)
            out += f"\nLo ultimo que hablasteis ({why}):\n{lines}\n"
        return out + self.conversation_context(session, text, skip={u for u, _ in recent})

    def conversation_context(self, session: str, text: str, skip: set[str] | None = None) -> str:
        """Conversaciones anteriores relacionadas con lo que se acaba de decir (para el prompt)."""
        if not self.turn_log:
            return ""
        from .turnlog import related_prompt

        try:
            return related_prompt(self.turn_log.related(keywords(text), skip=skip))
        except Exception:
            log.exception("no se pudo buscar en el registro de conversaciones")
            return ""

    def _confirm(
        self, text: str, session: str, speak: bool, timings: dict[str, int], ctx: ToolContext, emit: EventSink,
        pending: PendingAction,
    ) -> TurnResult:
        """El usuario ha dicho "si": se ejecuta la accion tal cual se propuso, sin volver a pasar por el LLM."""
        name = pending.tool.name
        emit({"type": "tool", "id": "confirm", "name": name, "args": _args(json.dumps(pending.args))})
        with _timed(timings, "tool"):
            ok, result = self.tools.run_confirmed(pending, ctx)
        emit({"type": "tool_result", "id": "confirm", "name": name, "ok": ok, "ms": timings["tool"], "text": _short(result)})
        reply = f"Hecho. {result}" if ok else f"No he podido hacerlo: {result.removeprefix('ERROR: ')}"
        emit({"type": "reply", "text": reply, "provider": None, "ms": 0})
        log.info("[%s] accion confirmada %s: %r", session, name, reply)
        if self.turn_log:
            self.turn_log.add(session, text, reply)
        history = self._history[session]
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        audio = None
        if speak:
            emit({"type": "speaking"})
            with _timed(timings, "tts"):
                audio = self.tts.synthesize(reply)
        timings["total"] = sum(timings.values())
        return TurnResult(text, reply, None, audio, timings, [name], ctx.pc_actions)

    def _respond(
        self, text: str, session: str, speak: bool, timings: dict[str, int], ctx: ToolContext, emit: EventSink,
        model: str | None = None,
    ) -> TurnResult:
        waiting = self._pending.pop(session, None)
        if waiting and self.tools and time.monotonic() < waiting[1]:
            if is_affirmative(text):
                return self._confirm(text, session, speak, timings, ctx, emit, waiting[0])
            if not is_negative(text):
                # Respuesta que no es un "si" claro: si el modelo entiende que si y vuelve a pedir exactamente la misma
                # accion en este turno, se hace (sin volver a preguntar y quedarse en bucle).
                ctx.preconfirmed = waiting[0]
            log.info("[%s] sin 'si' claro para: %s", session, waiting[0].summary)
        if self.direct_answers and not ctx.preconfirmed:
            hit = self._direct(text, session, speak, timings, ctx, emit)
            if hit:
                return hit
        self._resume_history(session)
        history = self._history[session]
        system = self.system_prompt
        in_context = {m["content"] for m in history if m["role"] == "user"}
        system += self.conversation_context(session, text, skip=in_context)
        news = self.take_news()
        if news:
            system += "\n\n" + news
        if self.memory:
            with _timed(timings, "memory"):
                recalled = self.memory.recall(text)
                system += as_prompt(recalled)
            emit(
                {
                    "type": "memory",
                    "ms": timings["memory"],
                    "items": [
                        {"id": r.memory.id, "kind": r.memory.type, "text": _short(r.memory.content, 80), "reason": r.reason}
                        for r in recalled
                    ],
                }
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": text},
        ]
        specs = self.tools.specs(ctx) if self.tools else []
        if specs and self.tool_filter:
            total = len(specs)
            keep = {ctx.preconfirmed.tool.name} if ctx.preconfirmed else set()
            specs, groups = select_specs(specs, text, self._last_groups.get(session), keep)
            self._last_groups[session] = groups
            if len(specs) < total:
                log.info("[%s] herramientas: %d de %d (%s)", session, len(specs), total, ", ".join(sorted(groups)))
        used: list[str] = []

        with _timed(timings, "llm"):
            for round_ in range(MAX_TOOL_ROUNDS):
                emit({"type": "thinking", "round": round_ + 1})
                reply = self.llm.chat(messages, specs or None, prefer=model)
                if not reply.tool_calls:
                    break
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply.text or None,
                        "tool_calls": [
                            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                            for c in reply.tool_calls
                        ],
                    }
                )
                for call in reply.tool_calls:
                    used.append(call.name)
                    emit({"type": "tool", "id": call.id, "name": call.name, "args": _args(call.arguments)})
                    start = time.perf_counter()
                    shown = len(ctx.cards)
                    result = self.tools.execute(call.name, call.arguments, ctx)
                    emit(
                        {
                            "type": "tool_result",
                            "id": call.id,
                            "name": call.name,
                            "ok": not result.startswith("ERROR"),
                            "ms": round((time.perf_counter() - start) * 1000),
                            "text": _short(result),
                        }
                    )
                    if len(ctx.cards) > shown:  # la ventana de resultados del HUD aparece ya
                        emit({"type": "cards", "cards": ctx.cards[shown:]})
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            else:
                # Sigue pidiendo tools: ultima vuelta sin ellas para forzar una respuesta.
                emit({"type": "thinking", "round": MAX_TOOL_ROUNDS + 1})
                reply = self.llm.chat(messages, prefer=model)
        emit({"type": "reply", "text": reply.text, "provider": reply.provider, "ms": timings["llm"]})
        if ctx.pending:
            self._pending[session] = (ctx.pending, time.monotonic() + PENDING_TTL_S)
            log.info("[%s] esperando confirmacion: %s", session, ctx.pending.summary)
        log.info("[%s] LLM %dms (%s) tools=%s: %r", session, timings["llm"], reply.provider, used, reply.text)
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply.text})
        if self.turn_log:
            self.turn_log.add(session, text, reply.text)
        if self.insights and emit is not _no_events:  # solo turnos del HUD (en directo)
            self.insights.submit(text, reply.text, used, model)

        audio = None
        if speak:
            emit({"type": "speaking"})
            with _timed(timings, "tts"):
                audio = self.tts.synthesize(reply.text)
            log.info("[%s] TTS %dms", session, timings["tts"])

        timings["total"] = sum(timings.values())
        return TurnResult(text, reply.text, reply.provider, audio, timings, used, ctx.pc_actions, ctx.cards)
