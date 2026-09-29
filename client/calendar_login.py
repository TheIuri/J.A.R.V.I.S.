"""Da permiso a JARVIS para CREAR eventos en tu calendario (una sola vez, en el PC).

    py calendar_login.py google     -> GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN

El permiso es solo para eventos del calendario (nada de correo ni archivos). Los valores que
imprime van en las variables de la app de TrueNAS; no los compartas ni los subas al repositorio.
Los pasos para crear la app de Google estan en el README ("Crear eventos").
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

GOOGLE_PORT = 8889
GOOGLE_REDIRECT = f"http://127.0.0.1:{GOOGLE_PORT}/"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"


def google() -> None:
    client_id = input("Client ID de Google: ").strip()
    client_secret = getpass.getpass("Client Secret (no se ve al escribir): ").strip()
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                result["error"] = "state no coincide (¿otra pestaña?)"
            elif "error" in query:
                result["error"] = query["error"][0]
            else:
                result["code"] = query.get("code", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Listo, ya puedes cerrar esta pestaña." if "code" in result else f"Error: {result.get('error')}"
            self.wfile.write(f"<h2>JARVIS · Google Calendar</h2><p>{msg}</p>".encode())
            done.set()

    server = HTTPServer(("127.0.0.1", GOOGLE_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": GOOGLE_REDIRECT, "response_type": "code", "scope": GOOGLE_SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Abriendo el navegador para iniciar sesión en Google...")
    print("(Si avisa de que la app no está verificada: «Configuración avanzada» → «Ir a ...». Es tu propia app.)")
    webbrowser.open(url)
    if not done.wait(300):
        sys.exit("Tiempo agotado (5 minutos).")
    server.shutdown()
    if "code" not in result:
        sys.exit(f"Google devolvió un error: {result.get('error')}")
    resp = httpx.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": GOOGLE_REDIRECT,
        "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier,
    }, timeout=15)
    token = resp.json().get("refresh_token") if resp.status_code == 200 else None
    if not token:
        sys.exit(f"No se pudo obtener el token ({resp.status_code}): {resp.text[:200]}")
    print("\nPon esto en las variables de la app de TrueNAS (y no lo compartas):\n")
    print(f'GOOGLE_CLIENT_ID: "{client_id}"')
    print('GOOGLE_CLIENT_SECRET: "(el que acabas de escribir)"')
    print(f'GOOGLE_REFRESH_TOKEN: "{token}"')


if __name__ == "__main__":
    which = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if which in ("", "google"):
        google()
    else:
        sys.exit("Uso: py calendar_login.py google")
