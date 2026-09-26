"""Cliente de escritorio para JARVIS (Windows/Linux/Mac): push-to-talk con Enter.

El cliente solo capta audio y lo reproduce; el cerebro vive en el servidor (TrueNAS).

Uso:
    python jarvis_client.py --server http://IP-DEL-NAS:8765 --token TU_TOKEN
    (o variables de entorno JARVIS_SERVER y JARVIS_TOKEN)

En el prompt:
    Enter vacio  -> empieza a grabar; Enter otra vez -> envia
    texto + Enter -> se lo envia escrito (responde con voz igualmente)
    /reset        -> olvida la conversacion actual
    /salir        -> termina
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import threading
import wave
from enum import Enum

import httpx
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # lo que espera Whisper


class State(Enum):
    IDLE = "esperando"
    RECORDING = "grabando"
    PROCESSING = "pensando"
    SPEAKING = "hablando"


def record_until_enter(input_device: int | None) -> bytes:
    chunks: list[np.ndarray] = []
    stop = threading.Event()

    def callback(indata, _frames, _time, status):
        if status:
            print(f"  (audio: {status})", file=sys.stderr)
        chunks.append(indata.copy())

    with sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=input_device, callback=callback
    ):
        threading.Thread(target=lambda: (input(), stop.set()), daemon=True).start()
        stop.wait()

    audio = np.concatenate(chunks) if chunks else np.zeros((0, 1), dtype="int16")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(audio.tobytes())
    return buf.getvalue()


def play_wav(data: bytes, output_device: int | None) -> None:
    with wave.open(io.BytesIO(data), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype="int16")
    if channels > 1:
        frames = frames.reshape(-1, channels)
    sd.play(frames, rate, device=output_device)
    sd.wait()


def show(state: State) -> None:
    print(f"[{state.value}]", flush=True)


def handle_response(resp: httpx.Response, output_device: int | None) -> None:
    if resp.status_code != 200:
        print(f"  Error {resp.status_code}: {resp.text}")
        return
    body = resp.json()
    if not body["transcript"]:
        print("  No te he oido bien, prueba otra vez.")
        return
    t = body["timings_ms"]
    print(f"  Tu:     {body['transcript']}")
    print(f"  Jarvis: {body['reply']}")
    print(f"  ({', '.join(f'{k} {v}ms' for k, v in t.items())} | {body['provider']})")
    if body.get("audio_wav_b64"):
        show(State.SPEAKING)
        play_wav(base64.b64decode(body["audio_wav_b64"]), output_device)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cliente push-to-talk de JARVIS")
    parser.add_argument("--server", default=os.environ.get("JARVIS_SERVER", "http://localhost:8765"))
    parser.add_argument("--token", default=os.environ.get("JARVIS_TOKEN", ""))
    parser.add_argument("--session", default="pc")
    parser.add_argument("--input-device", type=int, default=None, help="indice del microfono")
    parser.add_argument("--output-device", type=int, default=None, help="indice del altavoz (p.ej. Echo por Bluetooth)")
    parser.add_argument("--list-devices", action="store_true", help="muestra los dispositivos de audio y sale")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return
    if not args.token:
        sys.exit("Falta el token: usa --token o la variable JARVIS_TOKEN")

    client = httpx.Client(
        base_url=args.server.rstrip("/"), headers={"Authorization": f"Bearer {args.token}"}, timeout=60
    )
    try:
        health = client.get("/health").json()
    except httpx.HTTPError as exc:
        sys.exit(f"No puedo conectar con {args.server}: {exc}")
    print(f"Conectado. STT={health['stt']} | LLM={health['llm']} | TTS={health['tts']}")
    print("Enter para hablar, Enter para enviar. Tambien puedes escribir. /reset, /salir.\n")

    while True:
        show(State.IDLE)
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        try:
            if line in ("/salir", "/exit"):
                break
            if line == "/reset":
                client.post("/api/reset", json={"session": args.session})
                print("  Conversacion reiniciada.")
                continue
            if line:
                show(State.PROCESSING)
                resp = client.post("/api/chat", json={"text": line, "session": args.session})
            else:
                show(State.RECORDING)
                print("  Habla... (Enter para terminar)")
                audio = record_until_enter(args.input_device)
                show(State.PROCESSING)
                resp = client.post(
                    "/api/voice",
                    files={"audio": ("audio.wav", audio, "audio/wav")},
                    data={"session": args.session},
                )
            handle_response(resp, args.output_device)
        except httpx.HTTPError as exc:
            print(f"  Error de red: {exc}")
        except sd.PortAudioError as exc:
            print(f"  Error de audio: {exc}")


if __name__ == "__main__":
    main()
