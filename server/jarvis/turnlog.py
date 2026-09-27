"""Registro de conversaciones del dia para el resumen nocturno (Nivel 6).

Solo se activa con SUMMARY_AT. Se guarda en el NAS (SQLite) y se borra a los 7 dias.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KEEP_DAYS = 7
SKIP_SESSIONS = {"briefing"}  # el resumen matinal no es una conversacion tuya


class TurnLog:
    def __init__(self, path: Path | str, timezone: str = "Europe/Madrid"):
        self.tz = ZoneInfo(timezone)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS turns (ts TEXT NOT NULL, session TEXT, user TEXT, reply TEXT)")

    def add(self, session: str, user: str, reply: str) -> None:
        if session in SKIP_SESSIONS or not user.strip():
            return
        now = datetime.now(self.tz)
        with self._lock, self._db:
            self._db.execute("INSERT INTO turns VALUES (?, ?, ?, ?)", (now.isoformat(), session, user[:1000], reply[:2000]))
            self._db.execute("DELETE FROM turns WHERE ts < ?", ((now - timedelta(days=KEEP_DAYS)).isoformat(),))

    def day(self, day: date) -> list[tuple[str, str, str]]:
        """[(HH:MM, usuario, respuesta)] de ese dia."""
        start = datetime.combine(day, datetime.min.time(), self.tz)
        rows = self._db.execute(
            "SELECT ts, user, reply FROM turns WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start.isoformat(), (start + timedelta(days=1)).isoformat()),
        ).fetchall()
        return [(datetime.fromisoformat(ts).strftime("%H:%M"), u, r) for ts, u, r in rows]
