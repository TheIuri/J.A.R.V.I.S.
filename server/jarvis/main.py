"""API HTTP del core. Los clientes (PC, movil...) son solo microfono y altavoz."""

from __future__ import annotations

import base64
import json
import logging
import queue
import os
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .activity import ActivityLog
from .agent_history import AgentHistory
from .auditor import Auditor, audit_tool, parse_projects
from .agents import REPORT_FOLDER, SPECS, AgentTeam, agent_tools, extract_options, short_summary, split_report
from .claude_code import DEFAULT_MODELS as CLAUDE_DEFAULTS
from .claude_code import ClaudeCode, label
from .config import Settings, load_settings
from .insights import Insights
from .leads import STATUSES as LEAD_STATUSES, LeadStore, lead_tools
from .llm import FallbackLLM, LLMError, OpenAICompatLLM, usage_report
from .memory import MemoryRejected, MemoryStore, RuleRetriever
from .notify import NoticeBoard, NtfyPush, parse_quiet
from .obsidian import Vault
from .pipeline import PENDING_TTL_S, Assistant, TurnResult, is_affirmative
from .prompts import system_prompt
from .stt import FasterWhisperSTT, GroqSTT
from .outlook_login import OutlookLogin
from .tools import build_registry, calendar_writers, spotify_configured, truenas_snapshot
from .tools.calendar_write import calendar_add_tool
from .tools.info import research_tools
from .tools.calendar import Calendars, parse_calendars
from .tools.registry import ToolContext, ToolError
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
        system_prompt(settings.assistant_name, settings.home_city, memory=store is not None,
                      spotify=spotify_configured(settings)),
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
            leads, settings.leads_profile, settings.leads_profiles, AgentHistory(data / "agents_history.json"),
            data / "agent_models.json", data / "lead_profiles.json", data / "custom_agents.json",
        )
        assistant.team = team
        assistant.leads = leads
        for tool in agent_tools(team) + lead_tools(leads):
            tools.register(tool)
        log.info("Agentes: %s (informes en %s)", ", ".join(team.available), "Obsidian" if vault else "memoria")
    if settings.claude_token:
        store = assistant.memory

        def memories() -> list[str]:
            return [m.content for m in store.all()] if store else []

        assistant.claude = ClaudeCode(settings.claude_token, data / "claude", memories=memories,
                                      timezone=settings.timezone, models=settings.claude_models or CLAUDE_DEFAULTS)
        log.info("Claude (membresia) en el NAS: %s", "disponible" if assistant.claude.available else
                 "falta el programa claude en la imagen")
        if getattr(assistant, "team", None) is not None:
            assistant.team.claude = assistant.claude  # investigador, compras y captador pueden ir con Claude
        if assistant.claude.available and tools is not None:
            # El auditor de seguridad, aqui en el NAS con Claude (sustituye al del PC).
            assistant.auditor = Auditor(assistant.claude, truenas_snapshot(settings),
                                        parse_projects(settings.audit_projects), vault, assistant.board,
                                        assistant.activity, settings.timezone)
            tools.replace(audit_tool(assistant.auditor))
            log.info("Auditor en el NAS: proyectos %s", ", ".join(assistant.auditor.available_projects()))
    if settings.outlook_client_id and tools is not None:
        # Conectar Outlook desde el HUD (codigo de dispositivo): al terminar, calendar_add ya crea eventos en el.
        def outlook_connected() -> None:
            tools.replace(calendar_add_tool(calendar_writers(settings), settings.timezone))

        assistant.outlook = OutlookLogin(settings.outlook_client_id, settings.outlook_tenant, data / "outlook_token.json",
                                         settings.outlook_refresh_token, on_connected=outlook_connected)
    assistant.google_calendar = bool(settings.google_client_id and settings.google_refresh_token)
    assistant.only_claude = settings.hud_models == "claude"
    assistant.default_model = settings.default_model
    if getattr(assistant, "claude", None) is not None:
        assistant.claude.full = settings.claude_tools != "web" and tools is not None
        assistant.claude.system = assistant.system_prompt
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


class LeadProfileRequest(BaseModel):
    name: str
    text: str = ""  # "" = borrar lo guardado desde el HUD


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict = {}


class AgentRunRequest(BaseModel):
    agent: str
    task: str
    refresh: bool = False  # repetirlo aunque haya un informe parecido reciente


class CustomAgentRequest(BaseModel):
    name: str
    description: str = ""
    instructions: str
    model: str = ""


class AgentModelRequest(BaseModel):
    agent: str
    model: str = ""  # "" = la cadena del agente tal cual


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


INTERNAL_HOSTS = {"127.0.0.1", "::1"}
CLAUDE_EXCLUDED = {"web_read"}  # con tus datos a mano, Claude no lee webs (para eso, los agentes)  # la API interna (herramientas de Claude) solo desde dentro del contenedor


def wire_claude_tools(assistant: Assistant, internal_token: str) -> None:
    """Config MCP para que Claude (en la conversacion) pueda encargar trabajo a los agentes web de JARVIS."""
    claude = getattr(assistant, "claude", None)
    if claude is None or not claude.available:
        return
    if not claude.full and getattr(assistant, "team", None) is None:
        return  # modo web: solo sirve para lanzar agentes
    claude.home.mkdir(parents=True, exist_ok=True)
    path = claude.home / "mcp.json"
    port = os.environ.get("JARVIS_PORT", "8765")
    config = {"mcpServers": {"jarvis": {
        "command": sys.executable, "args": ["-m", "jarvis.mcp_agents"],
        "env": {"JARVIS_INTERNAL_URL": f"http://127.0.0.1:{port}", "JARVIS_INTERNAL_TOKEN": internal_token,
                "JARVIS_MCP_MODE": "full" if claude.full else "agents",
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    }}}
    path.write_text(json.dumps(config), encoding="utf-8")
    path.chmod(0o600)
    claude.mcp_config = path


def create_app(assistant: Assistant | None = None, api_token: str | None = None) -> FastAPI:
    state: dict = {"assistant": assistant, "token": api_token, "internal": secrets.token_urlsafe(32),
                   "claude_session": "default", "claude_cards": [], "claude_lock": threading.Lock()}
    if assistant is not None:
        wire_claude_tools(assistant, state["internal"])

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["assistant"] is None:
            settings = load_settings()
            state["token"] = settings.api_token
            state["assistant"] = build_assistant(settings)
            wire_claude_tools(state["assistant"], state["internal"])
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

    def require_internal(request: Request) -> None:
        """Solo el servidor MCP de Claude, desde dentro del contenedor, con el token de este arranque."""
        given = request.headers.get("X-Jarvis-Internal", "")
        host = request.client.host if request.client else ""
        if host not in INTERNAL_HOSTS or not secrets.compare_digest(given, state["internal"]):
            raise HTTPException(status_code=403, detail="Solo para uso interno")

    def web_team() -> AgentTeam:
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None:
            raise HTTPException(status_code=404, detail="Agentes desactivados")
        return team

    @app.get("/internal/agents", dependencies=[Depends(require_internal)], include_in_schema=False)
    def internal_agents() -> dict:
        team = web_team()
        return {"agents": [{"id": k, "description": team.specs[k].description} for k in team.web_agents()]}

    @app.post("/internal/agents/run", dependencies=[Depends(require_internal)], include_in_schema=False)
    def internal_agent_run(req: AgentRunRequest) -> dict:
        team = web_team()
        if not team.is_web(req.agent):  # nunca los agentes con datos privados
            raise HTTPException(status_code=400, detail=f"agente no disponible: {req.agent}")
        try:
            job, message = team.request(req.agent, req.task, req.refresh)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job": job.id if job else None, "message": message}

    @app.get("/internal/tools", dependencies=[Depends(require_internal)], include_in_schema=False)
    def internal_tools() -> dict:
        """Cerebro completo: las herramientas de JARVIS para Claude (sin las del PC ni leer webs)."""
        registry = state["assistant"].tools
        if registry is None:
            return {"tools": []}
        return {"tools": [
            {"name": f["function"]["name"], "description": f["function"]["description"],
             "inputSchema": f["function"]["parameters"]}
            for f in (spec for spec in registry.specs(ToolContext())) if f["function"]["name"] not in CLAUDE_EXCLUDED
        ]}

    @app.post("/internal/tools/call", dependencies=[Depends(require_internal)], include_in_schema=False)
    def internal_tool_call(req: ToolCallRequest) -> dict:
        a: Assistant = state["assistant"]
        if a.tools is None or req.name in CLAUDE_EXCLUDED:
            raise HTTPException(status_code=400, detail=f"herramienta no disponible: {req.name}")
        ctx = ToolContext()
        result = a.tools.execute(req.name, json.dumps(req.arguments), ctx)
        session = state["claude_session"]
        if ctx.pending:  # lo confirma el usuario en su siguiente mensaje, como con Groq
            a._pending[session] = (ctx.pending, time.monotonic() + PENDING_TTL_S)
        state["claude_cards"].extend(ctx.cards)
        return {"result": result}

    @app.get("/internal/agents/status", dependencies=[Depends(require_internal)], include_in_schema=False)
    def internal_agent_status() -> dict:
        team = web_team()
        jobs = [j for j in team.jobs.values() if team.is_web(j.agent)][-5:]
        lines = [f"[{j.id}] {team.specs[j.agent].label} · {j.topic}: {j.state}"
                 + (f": {j.summary}" if j.summary and j.state != "trabajando" else "") for j in jobs]
        return {"status": "\n".join(lines) or "No hay encargos a estos agentes."}

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

    def hunter() -> AgentTeam:
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None or "captador" not in team.available:
            raise HTTPException(status_code=404, detail="El captador de clientes no está disponible")
        return team

    @app.get("/api/leads/profiles", dependencies=[Depends(require_token)])
    def lead_profiles() -> dict:
        """Lo que ofreces, para que el captador busque el cliente adecuado (uno por producto o negocio)."""
        return {"profiles": hunter().profiles()}

    @app.post("/api/leads/profiles", dependencies=[Depends(require_token)])
    def save_lead_profile(req: LeadProfileRequest) -> dict:
        team = hunter()
        try:
            name = team.save_profile(req.name, req.text)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": name, "profiles": team.profiles()}

    @app.get("/api/calendar/status", dependencies=[Depends(require_token)])
    def calendar_status() -> dict:
        """Calendarios donde JARVIS puede crear eventos (y el estado de la conexion de Outlook desde el HUD)."""
        a = state["assistant"]
        outlook: OutlookLogin | None = getattr(a, "outlook", None)
        return {"google": bool(getattr(a, "google_calendar", False)),
                "outlook": outlook.status() if outlook else None}

    @app.post("/api/calendar/outlook/connect", dependencies=[Depends(require_token)])
    def calendar_outlook_connect() -> dict:
        outlook: OutlookLogin | None = getattr(state["assistant"], "outlook", None)
        if outlook is None:
            raise HTTPException(status_code=404, detail="Falta OUTLOOK_CLIENT_ID en la configuración")
        return outlook.start()

    @app.post("/api/agents/run", dependencies=[Depends(require_token)])
    def agent_run(req: AgentRunRequest) -> dict:
        """Encargo directo desde el HUD: sin pasar por el LLM de la conversacion (no gasta su cupo)."""
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None:
            raise HTTPException(status_code=404, detail="Agentes desactivados")
        try:
            job, message = team.request(req.agent, req.task, req.refresh)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job": job.id if job else None, "reused": job is None, "message": message}

    @app.post("/api/agents/custom", dependencies=[Depends(require_token)])
    def create_custom_agent(req: CustomAgentRequest) -> dict:
        """Nuevo agente personalizado (solo internet, maximo 8)."""
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None:
            raise HTTPException(status_code=404, detail="Agentes desactivados")
        try:
            return {"agent": team.create_agent(req.name, req.description, req.instructions, req.model)}
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/agents/custom/{key}", dependencies=[Depends(require_token)])
    def delete_custom_agent(key: str) -> dict:
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None:
            raise HTTPException(status_code=404, detail="Agentes desactivados")
        try:
            return {"deleted": team.delete_agent(key)}
        except ToolError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agents/model", dependencies=[Depends(require_token)])
    def agent_model(req: AgentModelRequest) -> dict:
        """Elegir desde el HUD el modelo que va primero para un agente (el resto de su cadena, de respaldo)."""
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None:
            raise HTTPException(status_code=404, detail="Agentes desactivados")
        try:
            team.set_model(req.agent, req.model)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"agent": req.agent, "prefer": team.prefer.get(req.agent, "")}

    @app.get("/api/agents", dependencies=[Depends(require_token)])
    def agents() -> dict:
        """Agentes disponibles, su cadena de modelos y sus tareas recientes (panel del HUD)."""
        team: AgentTeam | None = getattr(state["assistant"], "team", None)
        if team is None:
            return {"agents": [], "jobs": []}
        return {
            "agents": [
                {"id": k, "label": team.specs[k].label, "doing": team.specs[k].doing,
                 "description": team.specs[k].description, "custom": k in team.custom,
                 "models": team.llm_for(k).name, "prefer": team.prefer.get(k, ""),
                 "choices": team.model_choices(k),
                 **({"profiles": [*(["general"] if team.lead_profile else []), *team.lead_profiles]}
                    if k == "captador" else {})}
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
        out = {"chain": chain, "agents": agents, "models": usage_report(chain + agents)}
        if claude():
            out["claude"] = claude().usage()
        return out

    @app.get("/api/models", dependencies=[Depends(require_token)])
    def models() -> dict:
        """Modelos para el selector: los de la cadena y el resto del catalogo de cada proveedor."""
        return {"models": state["assistant"].llm.models(catalog=True)}

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

    def claude() -> ClaudeCode | None:
        c = getattr(state["assistant"], "claude", None)
        return c if c is not None and c.available else None

    @app.get("/hud/config", include_in_schema=False)
    def hud_config() -> dict:
        out: dict = {"mode": "server", "pc_apps": None}
        if getattr(state["assistant"], "default_model", ""):
            out["default_model"] = state["assistant"].default_model
        if claude():
            out["claude_models"] = claude().models()
            if getattr(state["assistant"], "only_claude", False):
                out["only_claude"] = True  # HUD_MODELS=claude: en CEREBRO solo Claude
        return out

    @app.post("/claude/chat/stream", dependencies=[Depends(require_token)])
    def claude_chat(req: ChatRequest) -> StreamingResponse:
        """Modo Claude: piensa Claude Code con la membresia, aqui en el NAS; la voz la pone JARVIS."""
        c = claude()
        if c is None:
            raise HTTPException(status_code=404, detail="Claude no está configurado en el servidor")
        text = req.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="Texto vacio")
        if req.model not in c.model_ids:
            raise HTTPException(status_code=400, detail="Modelo de Claude desconocido")
        a: Assistant = state["assistant"]
        events: queue.Queue = queue.Queue()

        def work() -> None:
            session = req.session[:40]
            waiting = a._pending.pop(session, None)
            if waiting and a.tools and time.monotonic() < waiting[1] and is_affirmative(text):
                # "Si" a una accion que propuso Claude: se hace tal cual, sin volver a preguntar a nadie.
                events.put({"type": "heard", "text": text, "ms": 0})
                with a._lock:
                    result = a._confirm(text, session, req.speak, {}, ToolContext(), events.put, waiting[0])
                events.put({"type": "done", **_to_json(result)})
                return
            events.put({"type": "heard", "text": text, "ms": 0})
            events.put({"type": "thinking", "round": 1})
            start = time.perf_counter()
            try:
                with state["claude_lock"]:  # las herramientas saben a que conversacion responder
                    state["claude_session"], state["claude_cards"] = session, []
                    reply, used = c.ask(text, req.model, session, events.put)
                    cards = list(state["claude_cards"])
            except (RuntimeError, ValueError, OSError) as exc:
                # Sin cupo o caido: contesta la cadena de JARVIS (Groq...) para no quedarte sin respuesta.
                log.warning("Claude no ha podido (%s); contesta %s", exc, a.llm.name)
                try:
                    result = a.handle_text(text, session, req.speak, on_event=events.put)
                    events.put({"type": "done", **_to_json(result)})
                except LLMError as exc2:
                    events.put({"type": "error", "detail": f"{exc} · respaldo: {exc2}"})
                return
            ms = round((time.perf_counter() - start) * 1000)
            name = label(req.model)
            if cards:
                events.put({"type": "cards", "cards": cards})
            events.put({"type": "reply", "text": reply, "provider": name, "ms": ms})
            audio, ms_tts = None, 0
            if req.speak:
                events.put({"type": "speaking"})
                start = time.perf_counter()
                try:
                    audio = a.speak(reply)
                except Exception:
                    log.exception("fallo la voz de la respuesta de Claude")
                ms_tts = round((time.perf_counter() - start) * 1000)
            events.put({
                "type": "done", "transcript": text, "reply": reply, "provider": name,
                "timings_ms": {"claude": ms, "tts": ms_tts, "total": ms + ms_tts}, "tools_used": used,
                "pc_actions": [], "cards": cards, "audio_wav_b64": _b64(audio),
            })

        def lines():
            threading.Thread(target=work, name="claude-turn", daemon=True).start()
            while True:
                event = events.get()
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["type"] in ("done", "error"):
                    return

        return StreamingResponse(
            lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        )

    @app.post("/api/reset", dependencies=[Depends(require_token)])
    def reset(req: SessionRequest) -> dict:
        state["assistant"].reset(req.session)
        if claude():
            claude().reset(req.session)
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
