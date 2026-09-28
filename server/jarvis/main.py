"""API HTTP del core. Los clientes (PC, movil...) son solo microfono y altavoz."""

from __future__ import annotations

import base64
import json
import logging
import queue
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .activity import ActivityLog
from .agents import REPORT_FOLDER, SPECS, AgentTeam, agent_tools, extract_options, short_summary, split_report
from .config import Settings, load_settings
from .insights import Insights
from .leads import STATUSES as LEAD_STATUSES, LeadStore, lead_tools
from .llm import FallbackLLM, LLMError, OpenAICompatLLM, usage_report
from .memory import MemoryRejected, MemoryStore, RuleRetriever
from .notify import NoticeBoard, NtfyPush, parse_quiet
from .obsidian import Vault
from .pipeline import Assistant, TurnResult
from .prompts import system_prompt
from .stt import FasterWhisperSTT, GroqSTT
from .tools import build_registry
from .tools.info import research_tools
from .tools.calendar import Calendars, parse_calendars
from .tools.registry import ToolError
from .tools.reminders import ReminderStore
from .tools.truenas import _default_connect
from .tools.vision import Vision, check_image
from .tts import EdgeTTS, NullTTS, PiperTTS
from .turnlog import TurnLog
from .watch import Watcher, briefing_check, calendar_check, reminders_check, summary_check, truenas_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # evita loguear URLs firmadas y ruido
log = logging.getLogger("jarvis")

MAX_AUDIO_BYTES = 10 * 1024 * 1024
NOTIFY_WAIT_MAX_S = 25
WEB_DIR = Path(__file__).with_name("web")  # HUD: lo usan el proxy del PC y el navegador del movil


def build_assistant(settings: Settings) -> Assistant:
    data = Path(settings.data_dir)
    if settings.stt_provider == "groq":
        stt = GroqSTT(settings.groq_api_key, settings.groq_stt_model, settings.language)
    elif settings.stt_provider == "local":
        stt = FasterWhisperSTT(
            settings.whisper_model,
            settings.whisper_device,
            settings.whisper_compute_type,
            settings.language,
            download_root=str(data / "whisper"),
        )
    else:
        raise ValueError(f"STT_PROVIDER desconocido: {settings.stt_provider!r}")

    llm = FallbackLLM(
        [OpenAICompatLLM(p, settings.llm_timeout_s, settings.llm_max_tokens) for p in settings.llm_providers]
    )
    if settings.tts_provider in ("piper", "edge"):
        tts = PiperTTS(settings.piper_voice, data / "piper", settings.piper_speaker, settings.piper_speed)
        if settings.tts_provider == "edge":
            tts = EdgeTTS(settings.edge_voice, settings.edge_rate, settings.edge_pitch, fallback=tts)
    else:
        tts = NullTTS()

    store = MemoryStore(data / "memory.db") if settings.memory_enabled else None
    vault = None
    if settings.obsidian_vault:
        vault = Vault(settings.obsidian_vault, settings.timezone, settings.obsidian_inbox, settings.obsidian_daily)
        log.info("Boveda de Obsidian: %s (%d notas)", vault.root, len(vault.notes()))
        if store and settings.obsidian_memory_note:
            store.on_change(lambda: vault.export_memory(store))
            vault.export_memory(store)
    calendars = Calendars(parse_calendars(settings.calendars), settings.timezone) if settings.calendars else None
    reminders = ReminderStore(data / "reminders.db", settings.timezone) if settings.notify_enabled else None
    vision = (
        Vision(FallbackLLM([OpenAICompatLLM(p, settings.llm_timeout_s, 512) for p in settings.vision_providers]))
        if settings.vision_providers
        else None
    )
    tools = build_registry(settings, store, vault, reminders, calendars, vision)
    retriever = RuleRetriever(store, settings.memory_max_items) if store else None

    log.info("STT=%s | LLM=%s | TTS=%s | memoria=%s", stt.name, llm.name, tts.name, "si" if store else "no")
    assistant = Assistant(
        stt,
        llm,
        tts,
        system_prompt(settings.assistant_name, settings.home_city, memory=store is not None),
        settings.history_turns,
        tools,
        retriever,
    )
    assistant.vault = vault
    assistant.activity = ActivityLog()  # trazabilidad de agentes para el HUD
    if settings.insights_enabled:
        assistant.insights = Insights(llm, assistant.activity)
    if reminders:
        assistant.board, assistant.watcher = build_watcher(settings, assistant, reminders, calendars)
    if tools and settings.agents_enabled:
        # Cada agente coge de aqui solo sus tools (todas de solo lectura; ver agents.py).
        available = {t.name: t for t in research_tools(settings.brave_api_key, settings.timezone)}
        for spec in SPECS.values():
            for name in spec.tools:
                if name not in available and (tool := tools.get(name)):
                    available[name] = tool
        # Informes largos: mas tokens de salida que una respuesta hablada.
        def chain(providers):
            return FallbackLLM(
                [OpenAICompatLLM(p, settings.llm_timeout_s * 2, max(4096, settings.llm_max_tokens)) for p in providers]
            )

        agent_llm = chain(settings.agent_llm_providers or settings.llm_providers)
        own = {key: chain(providers) for key, providers in settings.agent_models.items() if key in SPECS}
        leads = LeadStore(data / "leads.json")
        team = AgentTeam(
            agent_llm, available, vault, assistant.board, settings.timezone, own, assistant.activity,
            leads, settings.leads_profile, settings.leads_profiles,
        )
        assistant.team = team
        assistant.leads = leads
        for tool in agent_tools(team) + lead_tools(leads):
            tools.register(tool)
        log.info("Agentes: %s (informes en %s)", ", ".join(team.available), "Obsidian" if vault else "memoria")
    return assistant


def build_watcher(
    settings: Settings, assistant: Assistant, reminders: ReminderStore, calendars: Calendars | None
) -> tuple[NoticeBoard, Watcher]:
    """Avisos proactivos: tablon (voz + push opcional) y vigilantes de lo que este configurado."""
    push = NtfyPush(settings.ntfy_url, settings.ntfy_token) if settings.ntfy_url else None
    board = NoticeBoard(
        assistant.speak,
        settings.timezone,
        parse_quiet(settings.notify_quiet),
        push,
        Path(settings.data_dir) / "notices_seen.json",
    )
    checks = [reminders_check(reminders)]
    if calendars:
        checks.append(calendar_check(calendars, settings.calendar_remind_minutes))
    if settings.truenas_url and settings.truenas_api_key:
        checks.append(
            truenas_check(
                lambda: _default_connect(
                    settings.truenas_url, settings.truenas_user, settings.truenas_api_key, settings.truenas_verify_ssl
                ),
                settings.disk_temp_warn,
                settings.truenas_watch_minutes * 60,
            )
        )
    if settings.briefing_at:

        def ask(text: str) -> str:
            # Sesion propia: no mezcla el resumen con tu conversacion ni guarda historial.
            reply = assistant.handle_text(text, session="briefing", speak=False).reply
            assistant.reset("briefing")
            return reply

        checks.append(
            briefing_check(ask, settings.briefing_at, settings.timezone, settings.briefing_weekends, board.seen)
        )
    if settings.summary_at and assistant.vault:
        assistant.turn_log = TurnLog(Path(settings.data_dir) / "turns.db", settings.timezone)
        checks.append(
            summary_check(assistant.llm, assistant.turn_log, assistant.vault, settings.summary_at, settings.timezone,
                          board.seen)
        )
    return board, Watcher(board, checks)


class ChatRequest(BaseModel):
    text: str
    session: str = "default"
    speak: bool = True
    pc_apps: list[str] | None = None  # apps que el cliente de PC permite abrir; None = sin acciones de PC
    model: str | None = None  # proveedor elegido en el HUD (groq, gemini...); None = el orden configurado
    image: str | None = None  # foto de la camara del HUD (JPEG/PNG en base64), solo si esta encendida


class SpeakRequest(BaseModel):
    text: str


class AgentResult(BaseModel):
    title: str
    text: str
    source: str = "claude"


# Agentes externos (Claude Code en el PC): donde va su informe y como se llaman en el HUD.
EXTERNAL_AGENTS = {
    "claude": ("Claude", REPORT_FOLDER),
    "auditor": ("El auditor de seguridad", "JARVIS/Seguridad"),
}


class AgentEvent(BaseModel):
    type: str
    agent: str
    task: str = ""
    tool: str = ""
    model: str = ""


class LeadUpdate(BaseModel):
    id: int
    status: str
    note: str = ""


class SessionRequest(BaseModel):
    session: str = "default"


def _to_json(result: TurnResult) -> dict:
    return {
        "transcript": result.transcript,
        "reply": result.reply,
        "provider": result.provider,
        "timings_ms": result.timings_ms,
        "tools_used": result.tools_used,
        "pc_actions": result.pc_actions,
        "cards": result.cards,
        "audio_wav_b64": _b64(result.audio),
    }


def _b64(audio: bytes | None) -> str | None:
    return base64.b64encode(audio).decode() if audio else None


def _parse_apps(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [a.strip() for a in raw.split(",") if a.strip()]


def _image(b64: str | None) -> str | None:
    try:
        return check_image(b64)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _stream(fn, *args, model: str | None = None, image: str | None = None) -> StreamingResponse:
    """Ejecuta el turno en un hilo y manda cada evento del pipeline como una linea JSON (NDJSON).

    La ultima linea es {"type": "done", ...respuesta completa} o {"type": "error", "detail": ...}.
    """
    events: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            events.put({"type": "done", **_to_json(fn(*args, on_event=events.put, model=model, image=image))})
        except LLMError as exc:
            log.error("%s", exc)
            events.put({"type": "error", "detail": str(exc)})
        except Exception as exc:  # el cliente ya recibio 200: el error va dentro del flujo
            log.exception("fallo en el turno")
            events.put({"type": "error", "detail": f"fallo interno ({type(exc).__name__})"})

    def lines():
        threading.Thread(target=work, name="turn", daemon=True).start()
        while True:
            event = events.get()
            yield json.dumps(event, ensure_ascii=False) + "\n"
            if event["type"] in ("done", "error"):
                return

    # X-Accel-Buffering: que ningun proxy intermedio acumule las lineas.
    return StreamingResponse(
        lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
    )


def _read_audio(audio: UploadFile) -> bytes:
    data = audio.file.read(MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Audio vacio")
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio demasiado largo")
    return data


def create_app(assistant: Assistant | None = None, api_token: str | None = None) -> FastAPI:
    state: dict = {"assistant": assistant, "token": api_token}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["assistant"] is None:
            settings = load_settings()
            state["token"] = settings.api_token
            state["assistant"] = build_assistant(settings)
        watcher = getattr(state["assistant"], "watcher", None)
        if watcher:
            watcher.start()
        yield
        if watcher:
            watcher.stop()

    app = FastAPI(title="JARVIS core", version="0.1.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def require_token(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if creds is None or not secrets.compare_digest(creds.credentials, state["token"] or ""):
            raise HTTPException(status_code=401, detail="Token invalido")

    def run(fn, *args) -> dict:
        try:
            return _to_json(fn(*args))
        except LLMError as exc:
            log.error("%s", exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/health")
    def health() -> dict:
        a: Assistant = state["assistant"]
        return {
            "status": "ok",
            "stt": a.stt.name if a.stt else None,
            "llm": a.llm.name,
            "tts": a.tts.name,
            "tools": a.tools.names() if a.tools else [],
            "memory": a.memory is not None,
        }

    @app.post("/api/voice", dependencies=[Depends(require_token)])
    def voice(
        audio: UploadFile = File(...),
        session: str = Form("default"),
        speak: bool = Form(True),
        pc_apps: str | None = Form(None),  # separadas por comas
    ) -> dict:
        return run(state["assistant"].handle_audio, _read_audio(audio), session, speak, _parse_apps(pc_apps))

    @app.post("/api/chat", dependencies=[Depends(require_token)])
    def chat(req: ChatRequest) -> dict:
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="Texto vacio")
        return run(state["assistant"].handle_text, req.text, req.session, req.speak, req.pc_apps)

    # Igual que /api/voice y /api/chat, pero en directo: el HUD dibuja cada paso según ocurre.
    @app.post("/api/voice/stream", dependencies=[Depends(require_token)])
    def voice_stream(
        audio: UploadFile = File(...),
        session: str = Form("default"),
        speak: bool = Form(True),
        pc_apps: str | None = Form(None),
        model: str | None = Form(None),
        image: str | None = Form(None),
    ) -> StreamingResponse:
        data = _read_audio(audio)  # antes de responder: el fichero se cierra al acabar la peticion
        return _stream(
            state["assistant"].handle_audio, data, session, speak, _parse_apps(pc_apps),
            model=model or None, image=_image(image),
        )

    @app.post("/api/chat/stream", dependencies=[Depends(require_token)])
    def chat_stream(req: ChatRequest) -> StreamingResponse:
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="Texto vacio")
        return _stream(
            state["assistant"].handle_text, req.text, req.session, req.speak, req.pc_apps,
            model=req.model, image=_image(req.image),
        )

    @app.post("/api/agent_event", dependencies=[Depends(require_token)])
    def agent_event(ev: AgentEvent) -> dict:
        """El PC cuenta en directo lo que hace Claude Code (inicio y herramientas) para el cerebro del HUD."""
        if ev.agent not in EXTERNAL_AGENTS or ev.type not in ("agent_start", "agent_tool", "agent_error"):
            raise HTTPException(status_code=400, detail="Evento no admitido")
        log_: ActivityLog | None = getattr(state["assistant"], "activity", None)
        if log_:
            log_.emit(ev.type, agent=ev.agent, label=EXTERNAL_AGENTS[ev.agent][0], task=ev.task[:200],
                      tool=ev.tool[:40], model=ev.model[:40] or "Claude")
        return {"ok": True}

    @app.get("/api/activity", dependencies=[Depends(require_token)])
    def activity(after: int = -1, wait: float = 0) -> dict:
        """Lo que hacen los agentes, en directo (after=-1: solo el ultimo id; wait: espera larga, max 25 s)."""
        log_: ActivityLog | None = getattr(state["assistant"], "activity", None)
        if log_ is None:
            return {"events": [], "last": 0}
        if after < 0:
            return {"events": [], "last": log_.last_id}
        return {"events": log_.since(after, min(max(wait, 0), NOTIFY_WAIT_MAX_S)), "last": log_.last_id}

    @app.get("/api/agents", dependencies=[Depends(require_token)])
    def agents() -> dict:
        """Agentes disponibles, su cadena de modelos y sus tareas recientes (panel del HUD)."""
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None:
            return {"agents": [], "jobs": []}
        return {
            "agents": [
                {"id": k, "label": SPECS[k].label, "doing": SPECS[k].doing, "description": SPECS[k].description,
                 "models": team.llm_for(k).name}
                for k in team.available
            ],
            "jobs": [
                {"id": j.id, "agent": j.agent, "task": j.topic, "state": j.state, "model": j.model,
                 "steps": len(j.steps), "summary": j.summary, "note": j.note, "started": j.started.isoformat(),
                 "cards": j.cards}
                for j in list(team.jobs.values())[-10:]
            ],
        }

    @app.get("/api/insights", dependencies=[Depends(require_token)])
    def insights() -> dict:
        """Las ultimas fichas con los datos clave de las respuestas (las nuevas llegan por /api/activity)."""
        ins: Insights | None = getattr(state["assistant"], "insights", None)
        return {"insights": list(ins.recent) if ins else [], "enabled": ins is not None}

    def lead_store() -> LeadStore:
        store = getattr(state["assistant"], "leads", None)
        if store is None:
            raise HTTPException(status_code=404, detail="Leads desactivados (hacen falta los agentes)")
        return store

    @app.get("/api/leads", dependencies=[Depends(require_token)])
    def leads_list() -> dict:
        store = lead_store()
        return {"leads": list(reversed(store.leads)), "counts": store.counts(), "statuses": list(LEAD_STATUSES)}

    @app.post("/api/leads/update", dependencies=[Depends(require_token)])
    def leads_update(req: LeadUpdate) -> dict:
        try:
            return {"lead": lead_store().update(req.id, req.status, req.note)}
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/usage", dependencies=[Depends(require_token)])
    def usage() -> dict:
        """Cuota de cada modelo: lo gastado hoy, lo que queda segun el proveedor y si esta en su limite."""
        a: Assistant = state["assistant"]
        chain = [p.name for p in a.llm.providers]
        team: AgentTeam | None = getattr(a, "team", None)
        agents: list[str] = []
        if team is not None:
            for llm in [team.llm, *team.llms.values()]:
                agents += [p.name for p in llm.providers if p.name not in chain + agents]
        return {"chain": chain, "agents": agents, "models": usage_report(chain + agents)}

    @app.get("/api/models", dependencies=[Depends(require_token)])
    def models() -> dict:
        return {"models": state["assistant"].llm.models()}

    @app.post("/api/transcribe", dependencies=[Depends(require_token)])
    def transcribe(audio: UploadFile = File(...)) -> dict:
        return {"text": state["assistant"].transcribe(_read_audio(audio))}

    @app.post("/api/speak", dependencies=[Depends(require_token)])
    def speak(req: SpeakRequest) -> dict:
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="Texto vacio")
        return {"audio_wav_b64": _b64(state["assistant"].speak(req.text))}

    def memory_store() -> MemoryStore:
        a: Assistant = state["assistant"]
        if a.memory is None:
            raise HTTPException(status_code=404, detail="Memoria desactivada")
        return a.memory.store

    @app.get("/api/notifications", dependencies=[Depends(require_token)])
    def notifications(after: int = -1, wait: float = 0) -> dict:
        """Avisos proactivos. after=-1: solo el ultimo id (para empezar sin repetir los viejos).
        Con wait>0 espera hasta que haya uno nuevo (maximo 25 s)."""
        board: NoticeBoard | None = getattr(state["assistant"], "board", None)
        if board is None:
            return {"notices": [], "last": 0, "enabled": False}
        if after < 0:
            return {"notices": [], "last": board.last_id, "enabled": True}
        notices = board.since(after, min(max(wait, 0), NOTIFY_WAIT_MAX_S))
        return {"notices": [board.to_json(n) for n in notices], "last": board.last_id, "enabled": True}

    @app.post("/api/agent_result", dependencies=[Depends(require_token)])
    def agent_result(req: AgentResult) -> dict:
        """Informe de un agente externo (Claude Code en el PC): a Obsidian y aviso a todos los HUD."""
        if not req.text.strip() or len(req.text) > 20000:
            raise HTTPException(status_code=400, detail="Informe vacío o demasiado largo")
        a: Assistant = state["assistant"]
        if req.source not in EXTERNAL_AGENTS:
            raise HTTPException(status_code=400, detail="Agente desconocido")
        label, folder = EXTERNAL_AGENTS[req.source]
        cards, text = extract_options(req.text)
        summary, report = split_report(text)
        summary = short_summary(summary)
        title = " ".join(req.title.split())[:100] or "tarea"
        note = ""
        vault = getattr(a, "vault", None)
        if vault:
            day = datetime.now().strftime("%Y-%m-%d")
            header = f"> Tarea hecha por {label} con Claude (membresía) · {day}\n\n"
            try:
                note = vault.create(
                    f"{day} {title}"[:120], header + report[:15000], folder, check_secrets=False, max_chars=16000
                )
            except Exception as exc:  # sin nota, el aviso llega igual
                log.warning("no se pudo guardar el informe de %s: %s", req.source, exc)
        board = getattr(a, "board", None)
        if board:
            board.post("info", req.source, f"{label} ha terminado. {summary}")
        if getattr(a, "activity", None):
            a.activity.emit("agent_done", agent=req.source, label=label, summary=summary, note=note, model="Claude",
                            cards=cards)
        log.info("informe de %s recibido (%d caracteres) -> %s", req.source, len(req.text), note or "sin nota")
        return {"note": note, "summary": summary}

    @app.get("/api/memories", dependencies=[Depends(require_token)])
    def list_memories() -> dict:
        return {"memories": [m.__dict__ for m in memory_store().all()]}

    @app.delete("/api/memories/{memory_id}", dependencies=[Depends(require_token)])
    def delete_memory(memory_id: int) -> dict:
        try:
            m = memory_store().delete(memory_id)
        except MemoryRejected as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        log.info("recuerdo %d borrado a mano: %s", m.id, m.content)
        return {"deleted": m.__dict__}

    # --- HUD servido por el propio servidor (movil, via HTTPS de Tailscale) -------------
    # Los ficheros son publicos (no tienen secretos); la API sigue exigiendo el token,
    # que el navegador pide una vez y guarda en ese dispositivo.

    @app.get("/", include_in_schema=False)
    @app.get("/hud", include_in_schema=False)
    def hud_redirect() -> RedirectResponse:
        return RedirectResponse("/hud/")

    @app.get("/hud/config", include_in_schema=False)
    def hud_config() -> dict:
        return {"mode": "server", "pc_apps": None}

    @app.post("/api/reset", dependencies=[Depends(require_token)])
    def reset(req: SessionRequest) -> dict:
        state["assistant"].reset(req.session)
        return {"status": "ok"}

    @app.middleware("http")
    async def hud_no_cache(request, call_next):
        # Que el navegador compruebe siempre si hay version nueva del HUD (responde 304 si no cambio).
        response = await call_next(request)
        if request.url.path.startswith("/hud"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/hud", StaticFiles(directory=WEB_DIR, html=True), name="hud")
    return app


app = create_app()
