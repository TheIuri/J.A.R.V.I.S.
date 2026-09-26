"""Cliente de escritorio para JARVIS (Windows/Linux/Mac): push-to-talk con Enter.

El cliente solo capta audio y lo reproduce; el cerebro vive en el servidor (TrueNAS).

Uso:
    python jarvis_client.py --server http://IP-DEL-NAS:8765 --token TU_TOKEN
    (o variables de entorno JARVIS_SERVER y JARVIS_TOKEN)

En el prompt:
    Enter vacio  -> empieza a grabar; Enter otra vez -> envia
    texto + Enter -> se lo envia escrito (responde con voz igualmente)
    /reset        -> olvida la conversacion actual (no la memoria)
    /memoria      -> lista lo que JARVIS recuerda de ti
    /olvida N     -> borra el recuerdo numero N
    /salir        -> termina

Acciones en el PC (abrir apps, volumen, musica, temporizadores): solo las apps de apps.json.
Desactivalas con --no-actions.
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
from pathlib import Path

import httpx
import numpy as np
import sounddevice as sd

from pc_actions import PCActions, load_apps

SAMPLE_RATE = 16000  # lo que espera Whisper
_play_lock = threading.Lock()  # la voz principal y los avisos de temporizador no se pisan


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
    with _play_lock:
        sd.play(frames, rate, device=output_device)
        sd.wait()


def show(state: State) -> None:
    print(f"[{state.value}]", flush=True)


def handle_response(resp: httpx.Response, output_device: int | None, actions: PCActions | None) -> None:
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
    tools = f" | tools: {', '.join(body['tools_used'])}" if body.get("tools_used") else ""
    print(f"  ({', '.join(f'{k} {v}ms' for k, v in t.items())} | {body['provider']}{tools})")
    for action in body.get("pc_actions") or []:
        result = actions.run(action) if actions else "acciones desactivadas"
        print(f"  [PC] {action.get('action')}: {result}")
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
    parser.add_argument("--no-actions", action="store_true", help="no ejecutar acciones en este PC")
    parser.add_argument(
        "--apps", default=str(Path(__file__).with_name("apps.json")), help="lista de apps permitidas (JSON)"
    )
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
    print(f"Tools del servidor: {', '.join(health.get('tools') or []) or 'ninguna'}")
    print(f"Memoria: {'activada (/memoria para verla)' if health.get('memory') else 'desactivada'}")

    def announce(text: str) -> None:
        print(f"\n  [Aviso] {text}")
        try:
            audio = client.post("/api/speak", json={"text": text}).json().get("audio_wav_b64")
            if audio:
                play_wav(base64.b64decode(audio), args.output_device)
        except (httpx.HTTPError, sd.PortAudioError) as exc:
            print(f"  (no se pudo reproducir el aviso: {exc})")

    actions = None if args.no_actions else PCActions(load_apps(Path(args.apps)), announce)
    pc_apps = list(actions.apps) if actions else None
    if actions:
        print(f"Apps permitidas: {', '.join(pc_apps) or 'ninguna'} (edita {args.apps})")
    print("Enter para hablar, Enter para enviar. Tambien puedes escribir. /reset, /memoria, /olvida N, /salir.\n")

    while True:
        show(State.IDLE)
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        try:
            if line in ("/salir", "/exit"):
                if actions:
                    actions.cancel_all()
                break
            if line == "/reset":
                client.post("/api/reset", json={"session": args.session})
                print("  Conversacion reiniciada.")
                continue
            if line == "/memoria":
                resp = client.get("/api/memories")
                if resp.status_code != 200:
                    print(f"  {resp.json().get('detail', resp.text)}")
                    continue
                memories = resp.json()["memories"]
                for m in memories:
                    print(f"  [{m['id']}] ({m['type']}) {m['content']}")
                print(f"  {len(memories)} recuerdos.")
                continue
            if line.startswith("/olvida"):
                arg = line.removeprefix("/olvida").strip()
                if not arg.isdigit():
                    print("  Uso: /olvida N  (mira los numeros con /memoria)")
                    continue
                resp = client.delete(f"/api/memories/{arg}")
                detail = resp.json()
                print(f"  Olvidado: {detail['deleted']['content']}" if resp.status_code == 200 else f"  {detail.get('detail')}")
                continue
            if line:
                show(State.PROCESSING)
                resp = client.post("/api/chat", json={"text": line, "session": args.session, "pc_apps": pc_apps})
            else:
                show(State.RECORDING)
                print("  Habla... (Enter para terminar)")
                audio = record_until_enter(args.input_device)
                show(State.PROCESSING)
                resp = client.post(
                    "/api/voice",
                    files={"audio": ("audio.wav", audio, "audio/wav")},
                    data={"session": args.session, **({"pc_apps": ",".join(pc_apps)} if pc_apps is not None else {})},
                )
            handle_response(resp, args.output_device, actions)
        except httpx.HTTPError as exc:
            print(f"  Error de red: {exc}")
        except sd.PortAudioError as exc:
            print(f"  Error de audio: {exc}")


if __name__ == "__main__":
    main()
