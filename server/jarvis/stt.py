"""Voz a texto. Cada proveedor implementa `transcribe(audio_wav) -> str`."""

from __future__ import annotations

import io
import logging
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class STT(Protocol):
    name: str

    def transcribe(self, audio: bytes) -> str: ...


class FasterWhisperSTT:
    """Whisper local (faster-whisper). Usa la GPU si hay CUDA disponible."""

    def __init__(self, model_size: str, device: str, compute_type: str, language: str, download_root: str):
        from faster_whisper import WhisperModel  # import perezoso: pesa y no hace falta en tests

        self.name = f"faster-whisper:{model_size}"
        self.language = language
        log.info("Cargando Whisper %s (device=%s, compute_type=%s)", model_size, device, compute_type)
        self.model = WhisperModel(
            model_size, device=device, compute_type=compute_type, download_root=download_root
        )

    def transcribe(self, audio: bytes) -> str:
        segments, _info = self.model.transcribe(
            io.BytesIO(audio), language=self.language, beam_size=1, vad_filter=True
        )
        return " ".join(s.text.strip() for s in segments).strip()


class GroqSTT:
    """Whisper en la nube de Groq (capa gratuita, muy rapido)."""

    def __init__(self, api_key: str, model: str, language: str, timeout_s: int = 30):
        if not api_key:
            raise RuntimeError("STT_PROVIDER=groq requiere GROQ_API_KEY")
        self.name = f"groq:{model}"
        self.model = model
        self.language = language
        self._client = httpx.Client(
            base_url="https://api.groq.com/openai/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )

    def transcribe(self, audio: bytes) -> str:
        resp = self._client.post(
            "/audio/transcriptions",
            files={"file": ("audio.wav", audio, "audio/wav")},
            data={"model": self.model, "language": self.language, "response_format": "json"},
        )
        resp.raise_for_status()
        return resp.json().get("text", "").strip()
