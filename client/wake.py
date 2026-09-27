"""Palabra de activacion "Hey Jarvis" (Nivel 4) con openWakeWord, 100 % local en el PC.

El microfono se analiza aqui en bloques de 80 ms. Nada sale del PC hasta que se detecta la
palabra: entonces se graba lo que dices a continuacion hasta que te callas y ese audio se
entrega al HUD, que lo manda al servidor como si hubieras pulsado para hablar.

Instalacion:  py -m pip install -r requirements-wake.txt
"""

from __future__ import annotations

import io
import threading
import time
import wave
from dataclasses import dataclass
from typing import Callable

import numpy as np

RATE = 16000
BLOCK = 1280  # 80 ms: lo que espera openWakeWord
WAKE_MODEL = "hey_jarvis"


@dataclass
class EndpointConfig:
    start_timeout_s: float = 4.0  # si no empiezas a hablar en este tiempo, se cancela
    silence_s: float = 0.9  # silencio que marca el final de la frase
    max_s: float = 15.0  # frase mas larga admitida
    min_threshold: float = 0.012  # RMS minimo para considerar que hay voz


class Endpointer:
    """Decide cuando empieza y acaba la frase tras la palabra de activacion (VAD por energia)."""

    def __init__(self, noise_floor: float, cfg: EndpointConfig | None = None):
        self.cfg = cfg or EndpointConfig()
        self.threshold = max(self.cfg.min_threshold, noise_floor * 3)
        self.frames: list[np.ndarray] = []
        self.elapsed = 0.0
        self.silence = 0.0
        self.speaking = False

    def feed(self, frame: np.ndarray) -> str:
        """Devuelve "continue", "done" (frase completa) o "cancel" (no has dicho nada)."""
        self.frames.append(frame)
        dt = len(frame) / RATE
        self.elapsed += dt
        loud = rms(frame) >= self.threshold
        if loud:
            self.speaking = True
            self.silence = 0.0
        elif self.speaking:
            self.silence += dt
        if not self.speaking and self.elapsed >= self.cfg.start_timeout_s:
            return "cancel"
        if self.speaking and (self.silence >= self.cfg.silence_s or self.elapsed >= self.cfg.max_s):
            return "done"
        return "continue"

    def wav(self) -> bytes:
        return to_wav(np.concatenate(self.frames))


def rms(frame: np.ndarray) -> float:
    x = frame.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def to_wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


class WakeListener:
    """Hilo que escucha el microfono y avisa: on_wake() al oir "Hey Jarvis" y
    on_utterance(wav) / on_cancel() cuando termina (o no llega) la frase."""

    def __init__(
        self,
        on_wake: Callable[[], None],
        on_utterance: Callable[[bytes], None],
        on_cancel: Callable[[], None],
        threshold: float = 0.5,
        device: int | str | None = None,
    ):
        self.on_wake = on_wake
        self.on_utterance = on_utterance
        self.on_cancel = on_cancel
        self.threshold = threshold
        self.device = device
        self.paused = threading.Event()  # mientras JARVIS habla no se escucha (evita que se active solo)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def load_model():
        """Descarga (la primera vez) y carga el modelo. Lanza ImportError si falta openwakeword."""
        from openwakeword import utils
        from openwakeword.model import Model

        utils.download_models(model_names=[WAKE_MODEL])
        return Model(wakeword_models=[WAKE_MODEL], inference_framework="onnx")

    def start(self) -> None:
        import sounddevice as sd

        # En el hilo principal: si falta el modelo o el micro, el error se ve al arrancar.
        model = self.load_model()
        stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK, device=self.device)
        stream.start()
        self._thread = threading.Thread(target=self._run, args=(model, stream), name="wake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self, model, stream) -> None:
        try:
            self._listen(model, stream)
        except Exception as exc:  # micro desconectado, etc.: se avisa en vez de morir en silencio
            print(f'[wake] "Hey Jarvis" se ha detenido: {exc}')
        finally:
            stream.close()

    def _listen(self, model, stream) -> None:
        noise = 0.01
        endpointer: Endpointer | None = None
        cooldown_until = 0.0
        while not self._stop.is_set():
            block, _ = stream.read(BLOCK)
            frame = block[:, 0].copy()
            if endpointer is not None:
                state = endpointer.feed(frame)
                if state == "done":
                    self.on_utterance(endpointer.wav())
                elif state == "cancel":
                    self.on_cancel()
                if state != "continue":
                    endpointer = None
                    model.reset()
                    cooldown_until = time.monotonic() + 1.5
                continue
            if self.paused.is_set() or time.monotonic() < cooldown_until:
                model.reset()
                continue
            # Nivel de ruido de fondo (media lenta) para el umbral de voz.
            noise = 0.97 * noise + 0.03 * rms(frame)
            score = max(model.predict(frame).values())
            if score >= self.threshold:
                print(f"[wake] Hey Jarvis ({score:.2f})")
                self.on_wake()
                endpointer = Endpointer(noise)
