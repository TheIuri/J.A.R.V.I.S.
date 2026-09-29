"""Conectar Outlook desde el HUD (tambien desde el movil), sin el PC.

Flujo "device code" de Microsoft: el servidor pide un codigo, el HUD lo muestra, el usuario lo escribe en
microsoft.com/devicelogin (en cualquier dispositivo) y acepta. El servidor espera en segundo plano, recibe el
refresh token y lo guarda en el dataset (outlook_token.json), el mismo sitio que ya usa OutlookCalendar para
guardar los tokens renovados. El permiso es solo Calendars.ReadWrite (nada de correo ni archivos).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .tools.calendar_write import MS_LOGIN, OUTLOOK_SCOPE, saved_outlook_token

log = logging.getLogger(__name__)


class OutlookLogin:
    def __init__(self, client_id: str, tenant: str, token_file: Path, env_token: str = "",
                 on_connected: Callable[[], None] = lambda: None, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.client_id = client_id
        self.base = f"{MS_LOGIN}/{tenant or 'common'}/oauth2/v2.0"
        self.token_file = Path(token_file)
        self.env_token = env_token
        self.on_connected = on_connected
        self._client = client or httpx.Client(timeout=15)
        self._sleep = sleep
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"state": "idle"}

    @property
    def connected(self) -> bool:
        return bool(self.env_token or saved_outlook_token(self.token_file, self.env_token))

    def status(self) -> dict[str, Any]:
        public = {k: v for k, v in self.state.items() if not k.startswith("_")}
        return {**public, "connected": self.connected}

    def start(self, background: bool = True) -> dict[str, Any]:
        """Pide un codigo nuevo y se queda esperando a que el usuario acepte."""
        with self._lock:
            if self.state.get("state") == "pending" and time.monotonic() < self.state.get("_until", 0):
                return self.status()  # ya hay un codigo vigente: el mismo
            try:
                resp = self._client.post(f"{self.base}/devicecode",
                                         data={"client_id": self.client_id, "scope": OUTLOOK_SCOPE})
            except httpx.HTTPError as exc:
                self.state = {"state": "error", "detail": f"no puedo conectar con Microsoft ({type(exc).__name__})"}
                return self.status()
            if resp.status_code != 200:
                try:
                    reason = str(resp.json().get("error_description", "")).splitlines()[0][:160]
                except (ValueError, IndexError):
                    reason = ""
                self.state = {"state": "error", "detail": "Microsoft no acepta la app: revisa OUTLOOK_CLIENT_ID y que "
                              f"tenga activados los flujos de cliente público. {reason}".strip()}
                return self.status()
            flow = resp.json()
            expires = int(flow.get("expires_in", 900))
            self.state = {
                "state": "pending", "user_code": flow["user_code"],
                "verification_uri": flow.get("verification_uri", "https://microsoft.com/devicelogin"),
                "expires_in": expires, "_until": time.monotonic() + expires,
            }
        args = (flow["device_code"], int(flow.get("interval", 5)))
        if background:
            threading.Thread(target=self._wait, args=args, name="outlook-login", daemon=True).start()
        else:
            self._wait(*args)
        return self.status()

    def _wait(self, device_code: str, interval: int) -> None:
        until = self.state["_until"]
        while time.monotonic() < until:
            self._sleep(interval)
            try:
                data = self._client.post(f"{self.base}/token", data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": self.client_id,
                    "device_code": device_code,
                }).json()
            except (httpx.HTTPError, ValueError):
                continue
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error or not data.get("refresh_token"):
                reasons = {"authorization_declined": "has rechazado el permiso", "expired_token": "el código ha caducado"}
                self.state = {"state": "error",
                              "detail": reasons.get(error, str(data.get("error_description") or error or "sin token"))[:200]}
                return
            self._save(data["refresh_token"])
            self.state = {"state": "ok"}
            log.info("Outlook conectado desde el HUD")
            try:
                self.on_connected()
            except Exception:
                log.exception("no se pudo activar el calendario de Outlook")
            return
        self.state = {"state": "error", "detail": "el código ha caducado; pide otro"}

    def _save(self, refresh_token: str) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"origin": self.env_token[-12:], "refresh_token": refresh_token}), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.token_file)
