"""Pipeline del Nivel 1: audio -> STT -> LLM -> TTS, con latencia medida por etapa."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field

from .llm import FallbackLLM
from .stt import STT
from .tts import TTS

log = logging.getLogger(__name__)


@dataclass
class TurnResult:
    transcript: str
    reply: str
    provider: str | None
    audio: bytes | None
    timings_ms: dict[str, int] = field(default_factory=dict)


@contextmanager
def _timed(timings: dict[str, int], stage: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = round((time.perf_counter() - start) * 1000)


class Assistant:
    def __init__(self, stt: STT | None, llm: FallbackLLM, tts: TTS, system_prompt: str, history_turns: int = 6):
        self.stt = stt
        self.llm = llm
        self.tts = tts
        self.system_prompt = system_prompt
        # Contexto de la conversacion en curso, solo en RAM (la memoria persistente es el Nivel 3).
        self._history: dict[str, deque[dict[str, str]]] = defaultdict(lambda: deque(maxlen=history_turns * 2))
        # Un turno cada vez: evita pelearse por la GPU y mantiene el orden del historial.
        self._lock = threading.Lock()

    def handle_audio(self, audio: bytes, session: str = "default", speak: bool = True) -> TurnResult:
        if self.stt is None:
            raise RuntimeError("No hay proveedor STT configurado")
        with self._lock:
            timings: dict[str, int] = {}
            with _timed(timings, "stt"):
                transcript = self.stt.transcribe(audio)
            log.info("[%s] STT %dms: %r", session, timings["stt"], transcript)
            if not transcript:
                return TurnResult("", "", None, None, timings)
            return self._respond(transcript, session, speak, timings)

    def handle_text(self, text: str, session: str = "default", speak: bool = True) -> TurnResult:
        with self._lock:
            return self._respond(text.strip(), session, speak, {})

    def reset(self, session: str = "default") -> None:
        self._history.pop(session, None)

    def _respond(self, text: str, session: str, speak: bool, timings: dict[str, int]) -> TurnResult:
        history = self._history[session]
        messages = [{"role": "system", "content": self.system_prompt}, *history, {"role": "user", "content": text}]

        with _timed(timings, "llm"):
            reply = self.llm.chat(messages)
        log.info("[%s] LLM %dms (%s): %r", session, timings["llm"], reply.provider, reply.text)
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply.text})

        audio = None
        if speak:
            with _timed(timings, "tts"):
                audio = self.tts.synthesize(reply.text)
            log.info("[%s] TTS %dms", session, timings["tts"])

        timings["total"] = sum(timings.values())
        return TurnResult(text, reply.text, reply.provider, audio, timings)
