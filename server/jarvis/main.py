"""API HTTP del core. Los clientes (PC, movil...) son solo microfono y altavoz."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .config import Settings, load_settings
from .llm import FallbackLLM, LLMError, OpenAICompatLLM
from .memory import MemoryRejected, MemoryStore, RuleRetriever
from .obsidian import Vault
from .pipeline import Assistant, TurnResult
from .prompts import system_prompt
from .stt import FasterWhisperSTT, GroqSTT
from .tools import build_registry
from .tts import NullTTS, PiperTTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # evita loguear URLs firmadas y ruido
log = logging.getLogger("jarvis")

MAX_AUDIO_BYTES = 10 * 1024 * 1024
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
    tts = PiperTTS(settings.piper_voice, data / "piper") if settings.tts_provider == "piper" else NullTTS()

    store = MemoryStore(data / "memory.db") if settings.memory_enabled else None
    vault = None
    if settings.obsidian_vault:
        vault = Vault(settings.obsidian_vault, settings.timezone, settings.obsidian_inbox, settings.obsidian_daily)
        log.info("Boveda de Obsidian: %s (%d notas)", vault.root, len(vault.notes()))
        if store and settings.obsidian_memory_note:
            store.on_change(lambda: vault.export_memory(store))
            vault.export_memory(store)
    tools = build_registry(settings, store, vault)
    retriever = RuleRetriever(store, settings.memory_max_items) if store else None

    log.info("STT=%s | LLM=%s | TTS=%s | memoria=%s", stt.name, llm.name, tts.name, "si" if store else "no")
    return Assistant(
        stt,
        llm,
        tts,
        system_prompt(settings.assistant_name, settings.home_city, memory=store is not None),
        settings.history_turns,
        tools,
        retriever,
    )


class ChatRequest(BaseModel):
    text: str
    session: str = "default"
    speak: bool = True
    pc_apps: list[str] | None = None  # apps que el cliente de PC permite abrir; None = sin acciones de PC


class SpeakRequest(BaseModel):
    text: str


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
        "audio_wav_b64": _b64(result.audio),
    }


def _b64(audio: bytes | None) -> str | None:
    return base64.b64encode(audio).decode() if audio else None


def _parse_apps(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    return [a.strip() for a in raw.split(",") if a.strip()]


def create_app(assistant: Assistant | None = None, api_token: str | None = None) -> FastAPI:
    state: dict = {"assistant": assistant, "token": api_token}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["assistant"] is None:
            settings = load_settings()
            state["token"] = settings.api_token
            state["assistant"] = build_assistant(settings)
        yield

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
        data = audio.file.read(MAX_AUDIO_BYTES + 1)
        if not data:
            raise HTTPException(status_code=400, detail="Audio vacio")
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Audio demasiado largo")
        return run(state["assistant"].handle_audio, data, session, speak, _parse_apps(pc_apps))

    @app.post("/api/chat", dependencies=[Depends(require_token)])
    def chat(req: ChatRequest) -> dict:
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="Texto vacio")
        return run(state["assistant"].handle_text, req.text, req.session, req.speak, req.pc_apps)

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

    app.mount("/hud", StaticFiles(directory=WEB_DIR, html=True), name="hud")
    return app


app = create_app()
