"""Da permiso a JARVIS para CREAR eventos en tu calendario (una sola vez, en el PC).

    py calendar_login.py google     -> GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN
    py calendar_login.py outlook    -> OUTLOOK_CLIENT_ID / OUTLOOK_REFRESH_TOKEN

El permiso es solo para eventos del calendario (nada de correo ni archivos). Los valores que
imprime van en las variables de la app de TrueNAS; no los compartas ni los subas al repositorio.
Los pasos para crear la app de Google o de Microsoft estan en el README ("Crear eventos").
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

GOOGLE_PORT = 8889
GOOGLE_REDIRECT = f"http://127.0.0.1:{GOOGLE_PORT}/"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
MS_SCOPE = "Calendars.ReadWrite offline_access"


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


def outlook() -> None:
    client_id = input("Application (client) ID de Microsoft: ").strip()
    tenant = input("Tenant [common]: ").strip() or "common"
    base = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"
    resp = httpx.post(f"{base}/devicecode", data={"client_id": client_id, "scope": MS_SCOPE}, timeout=15)
    if resp.status_code != 200:
        sys.exit(f"Microsoft no acepta la app ({resp.status_code}): {resp.text[:300]}\n"
                 "¿Activaste «Allow public client flows» en Authentication?")
    flow = resp.json()
    print(f"\n1. Abre {flow['verification_uri']}\n2. Escribe este código: {flow['user_code']}\n"
          "3. Inicia sesión con tu cuenta de Outlook y acepta.\n")
    webbrowser.open(flow["verification_uri"])
    deadline = time.monotonic() + int(flow.get("expires_in", 900))
    interval = int(flow.get("interval", 5))
    while time.monotonic() < deadline:
        time.sleep(interval)
        token = httpx.post(f"{base}/token", data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": client_id,
            "device_code": flow["device_code"],
        }, timeout=15).json()
        error = token.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            sys.exit(f"Microsoft devolvió un error: {token.get('error_description', error)[:300]}")
        print("Pon esto en las variables de la app de TrueNAS (y no lo compartas):\n")
        print(f'OUTLOOK_CLIENT_ID: "{client_id}"')
        if tenant != "common":
            print(f'OUTLOOK_TENANT: "{tenant}"')
        print(f'OUTLOOK_REFRESH_TOKEN: "{token["refresh_token"]}"')
        return
    sys.exit("Tiempo agotado: vuelve a ejecutarlo.")


if __name__ == "__main__":
    which = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if which == "google":
        google()
    elif which == "outlook":
        outlook()
    else:
        sys.exit("Uso: py calendar_login.py google   |   py calendar_login.py outlook")
