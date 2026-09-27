"""Pipeline: audio -> STT -> LLM (+ tools) -> TTS, con latencia medida por etapa.

Cada paso se puede seguir en directo con `on_event` (el "flujo de pensamiento" del HUD):
  heard -> memory -> thinking -> tool / tool_result (por cada llamada) -> reply -> speaking
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field

from typing import Any, Callable

from .llm import FallbackLLM
from .memory import Retriever, as_prompt
from .stt import STT
from .tools import ToolContext, ToolRegistry
from .tts import TTS

log = logging.getLogger(__name__)

# Rondas maximas de "el modelo pide tool -> se ejecuta -> vuelve al modelo" por turno.
MAX_TOOL_ROUNDS = 4
# Lo que se ensena en el HUD de cada resultado de tool (el LLM recibe el resultado completo).
EVENT_TEXT_CHARS = 160

EventSink = Callable[[dict[str, Any]], None]


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
        # Un turno cada vez: evita pelearse por la GPU y mantiene el orden del historial.
        self._lock = threading.Lock()

    def handle_audio(
        self,
        audio: bytes,
        session: str = "default",
        speak: bool = True,
        pc_apps: list[str] | None = None,
        on_event: EventSink | None = None,
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
            return self._respond(transcript, session, speak, timings, ToolContext(pc_apps=pc_apps), emit)

    def handle_text(
        self,
        text: str,
        session: str = "default",
        speak: bool = True,
        pc_apps: list[str] | None = None,
        on_event: EventSink | None = None,
    ) -> TurnResult:
        emit = on_event or _no_events
        with self._lock:
            text = text.strip()
            emit({"type": "heard", "text": text, "ms": 0})
            return self._respond(text, session, speak, {}, ToolContext(pc_apps=pc_apps), emit)

    def speak(self, text: str) -> bytes | None:
        """Solo TTS (p. ej. el aviso de un temporizador del PC)."""
        with self._lock:
            return self.tts.synthesize(text)

    def reset(self, session: str = "default") -> None:
        self._history.pop(session, None)

    def _respond(
        self, text: str, session: str, speak: bool, timings: dict[str, int], ctx: ToolContext, emit: EventSink
    ) -> TurnResult:
        history = self._history[session]
        system = self.system_prompt
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
        used: list[str] = []

        with _timed(timings, "llm"):
            for round_ in range(MAX_TOOL_ROUNDS):
                emit({"type": "thinking", "round": round_ + 1})
                reply = self.llm.chat(messages, specs or None)
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
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            else:
                # Sigue pidiendo tools: ultima vuelta sin ellas para forzar una respuesta.
                emit({"type": "thinking", "round": MAX_TOOL_ROUNDS + 1})
                reply = self.llm.chat(messages)
        emit({"type": "reply", "text": reply.text, "provider": reply.provider, "ms": timings["llm"]})
        log.info("[%s] LLM %dms (%s) tools=%s: %r", session, timings["llm"], reply.provider, used, reply.text)
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply.text})

        audio = None
        if speak:
            emit({"type": "speaking"})
            with _timed(timings, "tts"):
                audio = self.tts.synthesize(reply.text)
            log.info("[%s] TTS %dms", session, timings["tts"])

        timings["total"] = sum(timings.values())
        return TurnResult(text, reply.text, reply.provider, audio, timings, used, ctx.pc_actions)
