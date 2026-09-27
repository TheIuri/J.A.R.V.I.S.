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

    def __init__(self, voice: str, voices_dir: Path, speaker: int | None = None, speed: float = 1.0):
        from piper import PiperVoice  # import perezoso

        if not 0.5 <= speed <= 2.0:
            raise ValueError("PIPER_SPEED debe estar entre 0.5 y 2.0")
        self.name = f"piper:{voice}" + (f"#{speaker}" if speaker is not None else "")
        self.voice = PiperVoice.load(str(ensure_piper_voice(voice, voices_dir)))
        speakers = self.voice.config.num_speakers
        if speaker is not None and not 0 <= speaker < speakers:
            raise ValueError(f"PIPER_SPEAKER={speaker} no existe en {voice} (tiene {speakers} locutor/es)")
        self.options = {"speaker_id": speaker, "length_scale": 1.0 / speed}

    def synthesize(self, text: str) -> bytes | None:
        text = clean_for_speech(text)
        if not text:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            if hasattr(self.voice, "synthesize_wav"):  # piper-tts >= 1.3
                from piper import SynthesisConfig

                self.voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(**self.options))
            else:
                self.voice.synthesize(text, wav)
        return buf.getvalue()


class EdgeTTS:
    """Voces neuronales de Microsoft Edge ("Álvaro", "Elvira"...): muy naturales y gratis.

    No es un servicio oficial: si falla (sin internet, Microsoft lo cambia...), habla el respaldo
    (Piper), asi JARVIS nunca se queda mudo. Edge devuelve MP3; se convierte a WAV para que todos
    los clientes lo reproduzcan igual.
    """

    TIMEOUT_S = 15

    def __init__(self, voice: str = "es-ES-AlvaroNeural", rate: str = "+0%", pitch: str = "+0Hz", fallback: TTS | None = None):
        import edge_tts  # noqa: F401 - falla al arrancar si no esta instalado

        if not re.fullmatch(r"[+-]\d{1,3}%", rate) or not re.fullmatch(r"[+-]\d{1,3}Hz", pitch):
            raise ValueError("EDGE_RATE debe ser como +10% y EDGE_PITCH como -5Hz")
        self.voice, self.rate, self.pitch = voice, rate, pitch
        self.fallback = fallback
        self.name = f"edge:{voice}" + (f" (respaldo {fallback.name})" if fallback else "")

    async def _mp3(self, text: str) -> bytes:
        import asyncio

        import edge_tts

        async def collect() -> bytes:
            audio = b""
            async for chunk in edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch).stream():
                if chunk["type"] == "audio":
                    audio += chunk["data"]
            return audio

        return await asyncio.wait_for(collect(), self.TIMEOUT_S)

    def synthesize(self, text: str) -> bytes | None:
        import asyncio

        text = clean_for_speech(text)
        if not text:
            return None
        try:
            mp3 = asyncio.run(self._mp3(text))
            if not mp3:
                raise RuntimeError("Edge no ha devuelto audio")
            return to_wav(mp3)
        except Exception as exc:  # sin voz de Edge, habla el respaldo
            log.warning("Edge TTS ha fallado (%s); uso el respaldo", type(exc).__name__)
            return self.fallback.synthesize(text) if self.fallback else None


def to_wav(audio: bytes, rate: int = 24000) -> bytes:
    """MP3 (o WAV/FLAC) -> WAV mono 16 bits."""
    import miniaudio

    decoded = miniaudio.decode(audio, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(decoded.samples.tobytes())
    return buf.getvalue()
