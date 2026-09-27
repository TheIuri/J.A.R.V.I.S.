"""Consigue el SPOTIFY_REFRESH_TOKEN para JARVIS (se hace una sola vez, en el PC).

1. En https://developer.spotify.com/dashboard crea una app ("Web API") con Redirect URI:
       http://127.0.0.1:8888/callback
2. Ejecuta:  py spotify_login.py   (te pide el Client ID y el Client Secret de esa app)
3. Se abre el navegador, aceptas, y el script imprime el refresh token. Ponlo junto al
   Client ID y el Secret en las variables de la app de TrueNAS. No lo subas al repositorio.
"""

from __future__ import annotations

import getpass
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"


def main() -> None:
    client_id = input("Client ID: ").strip()
    client_secret = getpass.getpass("Client Secret (no se ve al escribir): ").strip()
    state = secrets.token_urlsafe(16)
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if urlparse(self.path).path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
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
            self.wfile.write(f"<h2>JARVIS · Spotify</h2><p>{msg}</p>".encode())
            done.set()

    server = HTTPServer(("127.0.0.1", 8888), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "https://accounts.spotify.com/authorize?" + urlencode(
        {"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES, "state": state}
    )
    print("Abriendo el navegador para iniciar sesión en Spotify...")
    webbrowser.open(url)
    if not done.wait(300):
        sys.exit("Tiempo agotado (5 minutos).")
    server.shutdown()
    if "code" not in result:
        sys.exit(f"Spotify devolvió un error: {result.get('error')}")

    resp = httpx.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "authorization_code", "code": result["code"], "redirect_uri": REDIRECT},
        auth=(client_id, client_secret),
        timeout=15,
    )
    if resp.status_code != 200:
        sys.exit(f"No se pudo obtener el token ({resp.status_code}): {resp.text[:200]}")
    print("\nPon esto en las variables de la app de TrueNAS (y no lo compartas):\n")
    print(f'SPOTIFY_CLIENT_ID: "{client_id}"')
    print('SPOTIFY_CLIENT_SECRET: "(el que acabas de escribir)"')
    print(f'SPOTIFY_REFRESH_TOKEN: "{resp.json()["refresh_token"]}"')


if __name__ == "__main__":
    main()
