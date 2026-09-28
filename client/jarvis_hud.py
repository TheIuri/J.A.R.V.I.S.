"""HUD de JARVIS: interfaz web en tu PC.

Sirve la interfaz en http://localhost:8766 y reenvia las peticiones al servidor (NAS).
- El token nunca llega al navegador: lo anade este proceso.
- Las acciones del PC (apps, volumen, temporizadores) se ejecutan aqui, con la misma
  lista permitida que el cliente de consola (apps.json).
- Solo escucha en 127.0.0.1 y rechaza peticiones de otras webs (Host/Origin).
- "Hey Jarvis" (Nivel 4): si esta instalado openwakeword (requirements-wake.txt), escucha
  la palabra de activacion en local y pasa la frase al navegador.

Uso:
    py jarvis_hud.py            (usa JARVIS_SERVER y JARVIS_TOKEN)
    py jarvis_hud.py --no-actions --port 8766
    py jarvis_hud.py --no-wake                   (sin "Hey Jarvis")
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from claude_mode import MODELS as CLAUDE_MODELS
from claude_mode import ClaudeChat
from delegate import Delegate
from pc_actions import PCActions, load_apps

# La interfaz vive en el servidor (tambien la sirve el NAS para el movil); aqui se usa la copia del repo.
STATIC_DIR = Path(__file__).resolve().parent.parent / "server" / "jarvis" / "web"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}

# Rutas del servidor que el HUD puede usar (nada mas se reenvia).
PROXY_POST = {"/api/chat", "/api/voice", "/api/reset", "/api/transcribe"}
STREAM_POST = {"/api/chat/stream", "/api/voice/stream"}  # flujo de pensamiento en directo (NDJSON)
PROXY_GET = {"/api/memories", "/api/notifications", "/api/models", "/api/activity", "/api/agents", "/health"}
MEMORY_DELETE = re.compile(r"^/api/memories/\d+$")
MAX_BODY = 12 * 1024 * 1024
EVENTS_WAIT_S = 20  # espera larga: el navegador recibe los avisos al instante


class Hud:
    def __init__(self, server: str, token: str, apps_path: Path, actions_enabled: bool):
        self.api = httpx.Client(base_url=server.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=90)
        self._events: list[dict] = []
        self._cond = threading.Condition()
        delegate = Delegate(self.report, self.announce)
        self.actions = PCActions(load_apps(apps_path), self.announce, delegate) if actions_enabled else None
        self.wake = None  # WakeListener si "Hey Jarvis" esta activo
        self.claude = ClaudeChat(shutil.which("claude"), self.memories)  # modo Claude (membresia)

    @property
    def pc_apps(self) -> list[str] | None:
        return list(self.actions.apps) if self.actions else None

    def announce(self, text: str) -> None:
        """Aviso de un temporizador: se genera la voz y el navegador lo recoge en /hud/events."""
        audio = None
        try:
            audio = self.api.post("/api/speak", json={"text": text}).json().get("audio_wav_b64")
        except httpx.HTTPError as exc:
            print(f"(no se pudo generar la voz del aviso: {exc})")
        self.push({"type": "announce", "text": text, "audio_wav_b64": audio})

    def memories(self) -> list[str]:
        try:
            return [m["content"] for m in self.api.get("/api/memories").json().get("memories", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    def claude_turn(self, body: dict, write) -> None:
        """Modo Claude: piensa Claude Code en este PC; el NAS pone la voz. Eventos como /api/chat/stream."""
        text = str(body.get("text", "")).strip()
        session = str(body.get("session", "default"))[:40]
        model = str(body.get("model", ""))
        write({"type": "heard", "text": text, "ms": 0})
        write({"type": "thinking", "round": 1})
        start = time.perf_counter()
        try:
            reply, used = self.claude.ask(text, model, session, write)
        except (RuntimeError, ValueError, OSError) as exc:
            return write({"type": "error", "detail": str(exc)})
        ms_claude = round((time.perf_counter() - start) * 1000)
        write({"type": "reply", "text": reply, "provider": CLAUDE_MODELS[model][1], "ms": ms_claude})
        write({"type": "speaking"})
        start = time.perf_counter()
        audio = None
        if body.get("speak", True):
            try:
                audio = self.api.post("/api/speak", json={"text": reply}).json().get("audio_wav_b64")
            except (httpx.HTTPError, ValueError):
                audio = None
        ms_tts = round((time.perf_counter() - start) * 1000)
        write({
            "type": "done", "transcript": text, "reply": reply, "provider": CLAUDE_MODELS[model][1],
            "timings_ms": {"claude": ms_claude, "tts": ms_tts, "total": ms_claude + ms_tts},
            "tools_used": used, "pc_actions": [], "cards": [], "audio_wav_b64": audio,
        })

    def report(self, title: str, text: str) -> None:
        """Informe de Claude Code: el servidor lo guarda en Obsidian y avisa a todos los HUD (y al movil)."""
        try:
            self.api.post("/api/agent_result", json={"title": title, "text": text, "source": "claude"}).raise_for_status()
        except httpx.HTTPError as exc:
            print(f"(no se pudo entregar el informe de Claude: {exc})")
            self.announce("Claude ha terminado, pero no he podido guardar el informe.")

    def push(self, event: dict) -> None:
        with self._cond:
            self._events.append(event)
            self._cond.notify_all()

    def take_events(self, wait_s: float = 0) -> list[dict]:
        with self._cond:
            if not self._events and wait_s:
                self._cond.wait(wait_s)
            events, self._events = self._events, []
        return events

    def start_wake(self, threshold: float, device: str | None) -> None:
        from wake import WakeListener

        self.wake = WakeListener(
            on_wake=lambda: self.push({"type": "wake"}),
            on_utterance=lambda wav: self.push({"type": "utterance", "audio_wav_b64": base64.b64encode(wav).decode()}),
            on_cancel=lambda: self.push({"type": "wake_cancel"}),
            threshold=threshold,
            device=int(device) if device and device.isdigit() else device,
        )
        self.wake.start()

    def set_busy(self, busy: bool) -> None:
        """El navegador avisa mientras piensa/habla: asi JARVIS no se activa con su propia voz."""
        if self.wake:
            (self.wake.paused.set if busy else self.wake.paused.clear)()

    def run_actions(self, body: dict) -> dict:
        results = []
        for action in body.get("pc_actions") or []:
            result = self.actions.run(action) if self.actions else "acciones desactivadas"
            print(f"[PC] {action.get('action')}: {result}")
            results.append({"action": action.get("action"), "result": result})
        body["pc_results"] = results
        return body


def make_handler(hud: Hud, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "JarvisHUD"

        def log_message(self, fmt, *args):  # sin ruido por cada peticion
            pass

        # --- utilidades --------------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data).encode())

        def _trusted(self) -> bool:
            # Evita que otra web abierta en el navegador use el HUD (DNS rebinding / CSRF).
            if self.headers.get("Host") not in allowed_hosts:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin.removeprefix("http://") in allowed_hosts

        def _forward(self, method: str, path: str, body: bytes | None = None) -> None:
            headers = {}
            if body is not None and self.headers.get("Content-Type"):
                headers["Content-Type"] = self.headers["Content-Type"]
            try:
                resp = hud.api.request(method, path, content=body, headers=headers)
            except httpx.HTTPError as exc:
                self._json(502, {"detail": f"No puedo conectar con el servidor: {exc}"})
                return
            if resp.status_code == 200 and path in ("/api/chat", "/api/voice"):
                self._json(200, hud.run_actions(resp.json()))
            else:
                self._send(resp.status_code, resp.content, resp.headers.get("Content-Type", "application/json"))

        def _forward_stream(self, path: str, body: bytes) -> None:
            """Reenvia el flujo linea a linea; al llegar la respuesta final ejecuta las acciones del PC."""
            headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
            started = False
            try:
                with hud.api.stream("POST", path, content=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        ctype = resp.headers.get("Content-Type", "application/json")
                        return self._send(resp.status_code, resp.read(), ctype)
                    # Sin Content-Length: con HTTP/1.0 el cuerpo termina al cerrar la conexion.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    started = True
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        event = json.loads(line)
                        if event.get("type") == "done":
                            event = hud.run_actions(event)
                        self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                        self.wfile.flush()
            except httpx.HTTPError as exc:
                detail = f"No puedo conectar con el servidor: {exc}"
                if started:
                    self.wfile.write((json.dumps({"type": "error", "detail": detail}) + "\n").encode())
                else:
                    self._json(502, {"detail": detail})
            except (BrokenPipeError, ConnectionResetError):
                pass  # el navegador cerro la pagina a mitad de turno

        def _claude(self, body: bytes) -> None:
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self._json(400, {"detail": "JSON no valido"})
            if not str(data.get("text", "")).strip():
                return self._json(400, {"detail": "Texto vacio"})
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            def write(event: dict) -> None:
                self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()

            try:
                hud.claude_turn(data, write)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # --- rutas -------------------------------------------------------------

        def do_GET(self):
            if not self._trusted():
                return self._json(403, {"detail": "origen no permitido"})
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                path = "/index.html"
            static = STATIC_DIR / path.lstrip("/")
            if static.suffix in CONTENT_TYPES and static.parent == STATIC_DIR and static.is_file():
                return self._send(200, static.read_bytes(), CONTENT_TYPES[static.suffix])
            if path == "/hud/config":
                claude = [{"id": k, "label": v[1]} for k, v in CLAUDE_MODELS.items()] if hud.claude.exe else []
                return self._json(
                    200, {"mode": "local", "pc_apps": hud.pc_apps, "wake": hud.wake is not None, "claude_models": claude}
                )
            if path == "/hud/events":
                return self._json(200, {"events": hud.take_events(EVENTS_WAIT_S)})
            if path in PROXY_GET:
                return self._forward("GET", self.path)  # con la query (?after=...&wait=...)
            self._json(404, {"detail": "no encontrado"})

        def do_POST(self):
            if not self._trusted():
                return self._json(403, {"detail": "origen no permitido"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"detail": "demasiado grande"})
            body = self.rfile.read(length)
            if self.path == "/claude/chat/stream":
                return self._claude(body)
            if self.path == "/api/reset":
                hud.claude.reset(str(json.loads(body or b"{}").get("session", "default")))
            if self.path in PROXY_POST:
                return self._forward("POST", self.path, body)
            if self.path in STREAM_POST:
                return self._forward_stream(self.path, body)
            if self.path == "/hud/busy":
                hud.set_busy(bool(json.loads(body or b"{}").get("busy")))
                return self._json(200, {"ok": True})
            self._json(404, {"detail": "no encontrado"})

        def do_DELETE(self):
            if not self._trusted():
                return self._json(403, {"detail": "origen no permitido"})
            if MEMORY_DELETE.match(self.path):
                return self._forward("DELETE", self.path)
            self._json(404, {"detail": "no encontrado"})

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="HUD web de JARVIS")
    parser.add_argument("--server", default=os.environ.get("JARVIS_SERVER", "http://localhost:8765"))
    parser.add_argument("--token", default=os.environ.get("JARVIS_TOKEN", ""))
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-actions", action="store_true", help="no ejecutar acciones en este PC")
    parser.add_argument("--no-browser", action="store_true", help="no abrir el navegador automaticamente")
    parser.add_argument("--apps", default=str(Path(__file__).with_name("apps.json")))
    parser.add_argument("--no-wake", action="store_true", help='sin palabra de activacion "Hey Jarvis"')
    parser.add_argument("--wake-threshold", type=float, default=0.5, help="sensibilidad 0-1 (mas bajo = mas sensible)")
    parser.add_argument("--wake-device", default=None, help="microfono (numero o nombre; ver jarvis_client.py --list-devices)")
    args = parser.parse_args()
    if not args.token:
        sys.exit("Falta el token: usa --token o la variable JARVIS_TOKEN")

    hud = Hud(args.server, args.token, Path(args.apps), not args.no_actions)
    try:
        health = hud.api.get("/health").json()
    except httpx.HTTPError as exc:
        sys.exit(f"No puedo conectar con {args.server}: {exc}")
    print(f"Servidor OK: LLM={health['llm']} | tools={len(health.get('tools') or [])} | memoria={health.get('memory')}")
    if not args.no_wake:
        try:
            hud.start_wake(args.wake_threshold, args.wake_device)
            print('"Hey Jarvis" activo (dilo en ingles: "jei YAR-vis")')
        except ImportError:
            print('"Hey Jarvis" desactivado: instala requirements-wake.txt para usarlo')
        except Exception as exc:  # microfono ocupado, descarga fallida...
            print(f'"Hey Jarvis" desactivado: {exc}')

    if hud.actions:
        found = hud.actions.delegate.available()
        print(f"Claude Code (membresia): {'disponible' if found else 'no instalado (opcional)'}")
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(hud, args.port))
    url = f"http://localhost:{args.port}"
    print(f"HUD en {url}  (Ctrl+C para salir)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if hud.actions:
            hud.actions.cancel_all()
        if hud.wake:
            hud.wake.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
