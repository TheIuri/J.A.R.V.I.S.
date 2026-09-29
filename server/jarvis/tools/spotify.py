"""Spotify: poner una cancion, artista, album o playlist concretos y controlar la reproduccion.

Necesita una app en developer.spotify.com (SPOTIFY_CLIENT_ID/SECRET) y un refresh token que se
obtiene una vez con client/spotify_login.py (SPOTIFY_REFRESH_TOKEN). Controlar la reproduccion
exige Spotify Premium y un dispositivo con Spotify abierto (PC, movil, altavoz...).
SPOTIFY_DEVICE elige el dispositivo preferido por nombre si no hay ninguno sonando.
"""

from __future__ import annotations

import threading
import time
import unicodedata

import httpx

from .registry import Tool, ToolContext, ToolError

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
KINDS = {"cancion": "track", "artista": "artist", "album": "album", "playlist": "playlist"}


def music_card(ctx: ToolContext | None, item: dict, state: str) -> None:
    """Tarjeta para el HUD: portada, titulo, artista y enlace a Spotify."""
    if ctx is None or not item:
        return
    images = (item.get("album") or {}).get("images") or item.get("images") or []
    image = next((i.get("url", "") for i in images if str(i.get("url", "")).startswith("https://")), "")
    url = (item.get("external_urls") or {}).get("spotify", "")
    ctx.cards.append({
        "kind": "music", "title": str(item.get("name", ""))[:160],
        "artist": ", ".join(a.get("name", "") for a in item.get("artists", [])[:3])[:160],
        "album": str((item.get("album") or {}).get("name", ""))[:160],
        "image": image, "url": url if url.startswith("https://open.spotify.com/") else "", "state": state,
    })


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)).strip()


class Spotify:
    def __init__(
        self, client_id: str, client_secret: str, refresh_token: str, device: str = "", client: httpx.Client | None = None
    ):
        self.creds = (client_id, client_secret)
        self.refresh_token = refresh_token
        self.device = device
        self._client = client or httpx.Client(timeout=10)
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token[1]:
                return self._token[0]
            try:
                resp = self._client.post(
                    TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": self.refresh_token}, auth=self.creds
                )
            except httpx.HTTPError as exc:
                raise ToolError(f"no puedo conectar con Spotify ({type(exc).__name__})") from exc
            if resp.status_code != 200:
                raise ToolError("Spotify rechaza las credenciales; vuelve a ejecutar spotify_login.py")
            data = resp.json()
            self._token = (data["access_token"], time.monotonic() + data.get("expires_in", 3600) - 60)
            return self._token[0]

    def _api(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = self._client.request(
                method, f"{API}{path}", headers={"Authorization": f"Bearer {self._access_token()}"}, **kw
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"no puedo conectar con Spotify ({type(exc).__name__})") from exc
        if resp.status_code in (200, 201, 202, 204):
            return resp
        reason = ""
        try:
            reason = resp.json().get("error", {}).get("reason", "")
        except ValueError:
            pass
        if reason == "PREMIUM_REQUIRED" or resp.status_code == 403:
            raise ToolError("Spotify solo deja controlar la reproducción con una cuenta Premium")
        if reason == "NO_ACTIVE_DEVICE" or resp.status_code == 404:
            raise ToolError("no hay ningún dispositivo con Spotify abierto; ábrelo en el PC o en el móvil")
        raise ToolError(f"Spotify respondió {resp.status_code}")

    def _device_id(self) -> str | None:
        """Si nada esta sonando, usa SPOTIFY_DEVICE o el primer dispositivo disponible."""
        devices = self._api("GET", "/me/player/devices").json().get("devices", [])
        if not devices:
            raise ToolError("no hay ningún dispositivo con Spotify abierto; ábrelo en el PC o en el móvil")
        if any(d.get("is_active") for d in devices):
            return None
        wanted = [d for d in devices if self.device and _norm(self.device) in _norm(d.get("name", ""))]
        return (wanted or devices)[0]["id"]

    def play(self, query: str, kind: str, ctx: ToolContext | None = None) -> str:
        stype = KINDS[kind]
        found = self._api("GET", "/search", params={"q": query, "type": stype, "limit": 1, "market": "ES"}).json()
        items = found.get(f"{stype}s", {}).get("items") or []
        items = [i for i in items if i]
        if not items:
            raise ToolError(f"no encuentro {kind} '{query}' en Spotify")
        item = items[0]
        body = {"uris": [item["uri"]]} if stype == "track" else {"context_uri": item["uri"]}
        device = self._device_id()
        self._api("PUT", "/me/player/play", params={"device_id": device} if device else None, json=body)
        music_card(ctx, item, "sonando")
        who = ", ".join(a["name"] for a in item.get("artists", [])[:2])
        return f"Sonando {kind} {item['name']}" + (f" de {who}" if who and stype != "artist" else "") + "."

    def control(self, action: str, value: int | None = None) -> str:
        if action == "pausa":
            self._api("PUT", "/me/player/pause")
        elif action == "reanudar":
            device = self._device_id()
            self._api("PUT", "/me/player/play", params={"device_id": device} if device else None)
        elif action == "siguiente":
            self._api("POST", "/me/player/next")
        elif action == "anterior":
            self._api("POST", "/me/player/previous")
        elif action == "volumen":
            if value is None or not 0 <= value <= 100:
                raise ToolError("el volumen va de 0 a 100")
            self._api("PUT", "/me/player/volume", params={"volume_percent": value})
        elif action in ("aleatorio_si", "aleatorio_no"):
            self._api("PUT", "/me/player/shuffle", params={"state": str(action == "aleatorio_si").lower()})
        return f"Hecho: {action.replace('_', ' ')}" + (f" {value}%" if action == "volumen" else "") + "."

    def now_playing(self, ctx: ToolContext | None = None) -> str:
        resp = self._api("GET", "/me/player/currently-playing")
        if resp.status_code == 204 or not resp.content:
            return "No está sonando nada en Spotify."
        data = resp.json()
        item = data.get("item") or {}
        if not item:
            return "No está sonando nada en Spotify."
        who = ", ".join(a["name"] for a in item.get("artists", []))
        state = "sonando" if data.get("is_playing") else "en pausa"
        music_card(ctx, item, state)
        return f"{item.get('name', '?')} de {who} ({state})."


def spotify_tools(spotify: Spotify) -> list[Tool]:
    return [
        Tool(
            name="spotify_play",
            description=(
                "Pone en Spotify una canción, artista, álbum o playlist concretos. Para 'pon música de X' usa "
                "kind=artista."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Qué buscar (p. ej. 'Viva la vida Coldplay')"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                },
                "required": ["query", "kind"],
            },
            fn=lambda ctx, query, kind: spotify.play(query, kind, ctx),
        ),
        Tool(
            name="spotify_control",
            description="Controla Spotify: pausa, reanudar, siguiente, anterior, volumen (0-100) o modo aleatorio.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pausa", "reanudar", "siguiente", "anterior", "volumen", "aleatorio_si", "aleatorio_no"],
                    },
                    "value": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["action"],
            },
            fn=lambda _ctx, action, value=None: spotify.control(action, value),
        ),
        Tool(
            name="spotify_now_playing",
            description="Qué canción está sonando ahora en Spotify.",
            parameters={"type": "object", "properties": {}},
            fn=lambda ctx: spotify.now_playing(ctx),
        ),
    ]
