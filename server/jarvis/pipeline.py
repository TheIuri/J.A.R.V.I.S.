"""Pipeline: audio -> STT -> LLM (+ tools) -> TTS, con latencia medida por etapa."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field

from typing import Any

from .llm import FallbackLLM
from .memory import Retriever, as_prompt
from .stt import STT
from .tools import ToolContext, ToolRegistry
from .tts import TTS

log = logging.getLogger(__name__)

# Rondas maximas de "el modelo pide tool -> se ejecuta -> vuelve al modelo" por turno.
MAX_TOOL_ROUNDS = 4


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
        self, audio: bytes, session: str = "default", speak: bool = True, pc_apps: list[str] | None = None
    ) -> TurnResult:
        if self.stt is None:
            raise RuntimeError("No hay proveedor STT configurado")
        with self._lock:
            timings: dict[str, int] = {}
            with _timed(timings, "stt"):
                transcript = self.stt.transcribe(audio)
            log.info("[%s] STT %dms: %r", session, timings["stt"], transcript)
            if not transcript:
                return TurnResult("", "", None, None, timings)
            return self._respond(transcript, session, speak, timings, ToolContext(pc_apps=pc_apps))

    def handle_text(
        self, text: str, session: str = "default", speak: bool = True, pc_apps: list[str] | None = None
    ) -> TurnResult:
        with self._lock:
            return self._respond(text.strip(), session, speak, {}, ToolContext(pc_apps=pc_apps))

    def speak(self, text: str) -> bytes | None:
        """Solo TTS (p. ej. el aviso de un temporizador del PC)."""
        with self._lock:
            return self.tts.synthesize(text)

    def reset(self, session: str = "default") -> None:
        self._history.pop(session, None)

    def _respond(
        self, text: str, session: str, speak: bool, timings: dict[str, int], ctx: ToolContext
    ) -> TurnResult:
        history = self._history[session]
        system = self.system_prompt
        if self.memory:
            with _timed(timings, "memory"):
                system += as_prompt(self.memory.recall(text))
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": text},
        ]
        specs = self.tools.specs(ctx) if self.tools else []
        used: list[str] = []

        with _timed(timings, "llm"):
            for _ in range(MAX_TOOL_ROUNDS):
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
                    result = self.tools.execute(call.name, call.arguments, ctx)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            else:
                # Sigue pidiendo tools: ultima vuelta sin ellas para forzar una respuesta.
                reply = self.llm.chat(messages)
        log.info("[%s] LLM %dms (%s) tools=%s: %r", session, timings["llm"], reply.provider, used, reply.text)
        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply.text})

        audio = None
        if speak:
            with _timed(timings, "tts"):
                audio = self.tts.synthesize(reply.text)
            log.info("[%s] TTS %dms", session, timings["tts"])

        timings["total"] = sum(timings.values())
        return TurnResult(text, reply.text, reply.provider, audio, timings, used, ctx.pc_actions)
