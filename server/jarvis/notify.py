"""Avisos proactivos (Nivel 4): JARVIS habla sin que le pregunten.

Los vigilantes (watch.py) publican avisos en el tablon. Los HUD los recogen con espera larga
(/api/notifications) y los dicen en voz alta; opcionalmente tambien llegan como notificacion
push al movil con ntfy (NTFY_URL), aunque el HUD este cerrado.

- Cada aviso lleva una clave: el mismo aviso no se repite (tampoco tras reiniciar).
- Horas de silencio (NOTIFY_QUIET="23:00-08:00"): se ven pero no se dicen, salvo los criticos.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("jarvis.notify")

LEVELS = ("info", "warning", "critical")
MAX_NOTICES = 50
MAX_SEEN = 1000


@dataclass(frozen=True)
class Notice:
    id: int
    created: str
    level: str
    source: str
    text: str
    speak: bool
    audio_wav_b64: str | None = None


def parse_quiet(raw: str) -> tuple[time, time] | None:
    if not raw:
        return None
    try:
        start, end = (time.fromisoformat(p.strip()) for p in raw.split("-"))
    except ValueError as exc:
        raise ValueError("NOTIFY_QUIET debe ser como 23:00-08:00") from exc
    return start, end


def in_quiet(now: time, quiet: tuple[time, time] | None) -> bool:
    if not quiet:
        return False
    start, end = quiet
    return start <= now < end if start <= end else now >= start or now < end


class NtfyPush:
    """Notificacion push al movil con ntfy (app gratuita; ntfy.sh o tu propio servidor)."""

    PRIORITY = {"info": "3", "warning": "4", "critical": "5"}

    def __init__(self, url: str, token: str = "", client: httpx.Client | None = None):
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.Client(timeout=8)

    def __call__(self, notice: Notice, quiet: bool) -> None:
        priority = "2" if quiet and notice.level != "critical" else self.PRIORITY[notice.level]
        # En la URL y no en cabeceras: las cabeceras HTTP no admiten acentos.
        params = {
            "title": f"JARVIS · {notice.source}",
            "priority": priority,
            "tags": {"info": "robot", "warning": "warning", "critical": "rotating_light"}[notice.level],
        }
        try:
            self._client.post(self.url, params=params, content=notice.text.encode(), headers=self.headers).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("no se pudo enviar el aviso a ntfy: %s", type(exc).__name__)


class NoticeBoard:
    def __init__(
        self,
        speak: Callable[[str], bytes | None] | None = None,
        timezone: str = "Europe/Madrid",
        quiet: tuple[time, time] | None = None,
        push: Callable[[Notice, bool], None] | None = None,
        seen_path: Path | None = None,
    ):
        self.speak = speak
        self.tz = ZoneInfo(timezone)
        self.quiet = quiet
        self.push = push
        self.seen_path = seen_path
        self._notices: deque[Notice] = deque(maxlen=MAX_NOTICES)
        self._seen: deque[str] = deque(self._load_seen(), maxlen=MAX_SEEN)
        self._next_id = 1
        self._cond = threading.Condition()

    def _load_seen(self) -> list[str]:
        try:
            return json.loads(self.seen_path.read_text()) if self.seen_path and self.seen_path.exists() else []
        except (OSError, ValueError):
            return []

    def _save_seen(self) -> None:
        if not self.seen_path:
            return
        try:
            tmp = self.seen_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(self._seen)))
            tmp.replace(self.seen_path)
        except OSError as exc:
            log.warning("no se pudo guardar %s: %s", self.seen_path, exc)

    def post(self, level: str, source: str, text: str, key: str | None = None) -> Notice | None:
        """Publica un aviso. Devuelve None si esa clave ya se aviso antes."""
        if level not in LEVELS:
            raise ValueError(f"nivel de aviso desconocido: {level}")
        with self._cond:
            if key and key in self._seen:
                return None
            if key:
                self._seen.append(key)
                self._save_seen()
        now = datetime.now(self.tz)
        quiet = in_quiet(now.time(), self.quiet)
        speak = level == "critical" or not quiet
        audio = None
        if speak and self.speak:
            try:
                wav = self.speak(text)
                audio = base64.b64encode(wav).decode() if wav else None
            except Exception:  # sin voz, el aviso llega igual por texto
                log.exception("no se pudo generar la voz del aviso")
        with self._cond:
            notice = Notice(self._next_id, now.isoformat(timespec="seconds"), level, source, text, speak, audio)
            self._next_id += 1
            self._notices.append(notice)
            self._cond.notify_all()
        log.info("aviso %d [%s/%s]%s: %s", notice.id, level, source, "" if speak else " (silencio)", text)
        if self.push:
            self.push(notice, quiet)
        return notice

    @property
    def last_id(self) -> int:
        return self._next_id - 1

    def since(self, after: int, wait_s: float = 0) -> list[Notice]:
        with self._cond:
            if wait_s and self.last_id <= after:
                self._cond.wait(wait_s)
            return [n for n in self._notices if n.id > after]

    @staticmethod
    def to_json(notice: Notice) -> dict:
        return asdict(notice)
