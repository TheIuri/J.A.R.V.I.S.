"""API HTTP del core. Los clientes (PC, movil...) son solo microfono y altavoz."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .config import Settings, load_settings
from .llm import FallbackLLM, LLMError, OpenAICompatLLM
from .pipeline import Assistant, TurnResult
from .prompts import system_prompt
from .stt import FasterWhisperSTT, GroqSTT
from .tts import NullTTS, PiperTTS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # evita loguear URLs firmadas y ruido
log = logging.getLogger("jarvis")

MAX_AUDIO_BYTES = 10 * 1024 * 1024


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

    log.info("STT=%s | LLM=%s | TTS=%s", stt.name, llm.name, tts.name)
    return Assistant(stt, llm, tts, system_prompt(settings.assistant_name), settings.history_turns)


class ChatRequest(BaseModel):
    text: str
    session: str = "default"
    speak: bool = True


class SessionRequest(BaseModel):
    session: str = "default"


def _to_json(result: TurnResult) -> dict:
    return {
        "transcript": result.transcript,
        "reply": result.reply,
        "provider": result.provider,
        "timings_ms": result.timings_ms,
        "audio_wav_b64": base64.b64encode(result.audio).decode() if result.audio else None,
    }


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
        }

    @app.post("/api/voice", dependencies=[Depends(require_token)])
    def voice(audio: UploadFile = File(...), session: str = Form("default"), speak: bool = Form(True)) -> dict:
        data = audio.file.read(MAX_AUDIO_BYTES + 1)
        if not data:
            raise HTTPException(status_code=400, detail="Audio vacio")
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Audio demasiado largo")
        return run(state["assistant"].handle_audio, data, session, speak)

    @app.post("/api/chat", dependencies=[Depends(require_token)])
    def chat(req: ChatRequest) -> dict:
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="Texto vacio")
        return run(state["assistant"].handle_text, req.text, req.session, req.speak)

    @app.post("/api/reset", dependencies=[Depends(require_token)])
    def reset(req: SessionRequest) -> dict:
        state["assistant"].reset(req.session)
        return {"status": "ok"}

    return app


app = create_app()
