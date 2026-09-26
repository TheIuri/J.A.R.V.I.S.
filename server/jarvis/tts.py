"""Texto a voz. Cada proveedor implementa `synthesize(text) -> wav bytes | None`."""

from __future__ import annotations

import io
import logging
import re
import wave
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger(__name__)

PIPER_VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


class TTS(Protocol):
    name: str

    def synthesize(self, text: str) -> bytes | None: ...


class NullTTS:
    name = "none"

    def synthesize(self, text: str) -> bytes | None:
        return None


def clean_for_speech(text: str) -> str:
    """Quita restos de markdown que el LLM cuele pese al prompt."""
    text = re.sub(r"[*_#`>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _voice_url(voice: str, suffix: str) -> str:
    # es_ES-davefx-medium -> es/es_ES/davefx/medium/es_ES-davefx-medium.onnx
    locale, speaker, quality = voice.split("-", 2)
    lang = locale.split("_")[0]
    return f"{PIPER_VOICES_URL}/{lang}/{locale}/{speaker}/{quality}/{voice}{suffix}"


def ensure_piper_voice(voice: str, voices_dir: Path) -> Path:
    voices_dir.mkdir(parents=True, exist_ok=True)
    model = voices_dir / f"{voice}.onnx"
    for suffix in (".onnx", ".onnx.json"):
        target = voices_dir / f"{voice}{suffix}"
        if target.exists():
            continue
        url = _voice_url(voice, suffix)
        log.info("Descargando voz Piper %s", url)
        tmp = target.with_suffix(target.suffix + ".part")
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        tmp.rename(target)
    return model


class PiperTTS:
    """Piper local: rapido incluso solo con CPU."""

    def __init__(self, voice: str, voices_dir: Path):
        from piper import PiperVoice  # import perezoso

        self.name = f"piper:{voice}"
        self.voice = PiperVoice.load(str(ensure_piper_voice(voice, voices_dir)))

    def synthesize(self, text: str) -> bytes | None:
        text = clean_for_speech(text)
        if not text:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            if hasattr(self.voice, "synthesize_wav"):  # piper-tts >= 1.3
                self.voice.synthesize_wav(text, wav)
            else:
                self.voice.synthesize(text, wav)
        return buf.getvalue()
