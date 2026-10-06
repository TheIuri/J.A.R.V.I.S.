"""Registro de conversaciones: la memoria de lo que habeis hablado.

Siempre activo. Se guarda en el NAS (SQLite, en el dataset) y se borra a los 30 dias. Sirve para:
- seguir la conversacion tras reiniciar o actualizar JARVIS (los ultimos turnos de la sesion);
- recordar conversaciones anteriores relacionadas con lo que preguntas («¿te acuerdas de lo del evento?»);
- el resumen nocturno en Obsidian (SUMMARY_AT);
- aprender: cada turno guarda que modelo contesto, con que herramientas y cuanto tardo, y tu pulgar arriba o
  abajo. Sin esa senal no hay forma de saber que funciona, asi que es la base del repaso semanal y de los
  atajos que se aprenden solos.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

KEEP_DAYS = 30
RECENT_HOURS = 12  # tras un reinicio se retoma la conversacion si es de las ultimas horas
SKIP_SESSIONS = {"briefing"}  # el resumen matinal no es una conversacion tuya


class TurnLog:
    def __init__(self, path: Path | str, timezone: str = "Europe/Madrid"):
        self.tz = ZoneInfo(timezone)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS turns (ts TEXT NOT NULL, session TEXT, user TEXT, reply TEXT)")
            # Los registros de antes no tienen estas columnas: se anaden sin perder nada.
            have = {row["name"] for row in self._db.execute("PRAGMA table_info(turns)")}
            for name, kind in (("id", "INTEGER"), ("model", "TEXT"), ("tools", "TEXT"), ("ms", "INTEGER"),
                               ("verdict", "TEXT"), ("note", "TEXT")):
                if name not in have:
                    self._db.execute(f"ALTER TABLE turns ADD COLUMN {name} {kind}")
            self._db.execute("CREATE INDEX IF NOT EXISTS turns_id ON turns (id)")

    def add(self, session: str, user: str, reply: str, model: str = "", tools: list[str] | None = None,
            ms: int = 0) -> int:
        """Guarda el turno y devuelve su numero, con el que luego se le pone el pulgar."""
        if session in SKIP_SESSIONS or not user.strip():
            return 0
        now = datetime.now(self.tz)
        with self._lock, self._db:
            row = self._db.execute("SELECT MAX(id) AS last FROM turns").fetchone()
            turn = (row["last"] or 0) + 1
            self._db.execute(
                "INSERT INTO turns (ts, session, user, reply, id, model, tools, ms, verdict, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '')",
                (now.isoformat(), session, user[:1000], reply[:2000], turn, model[:60],
                 ",".join(tools or [])[:200], int(ms)),
            )
            self._db.execute("DELETE FROM turns WHERE ts < ?", ((now - timedelta(days=KEEP_DAYS)).isoformat(),))
        return turn

    def rate(self, turn: int, verdict: str, note: str = "") -> dict | None:
        """Pulgar arriba ("bien") o abajo ("mal"), con lo que hubiera fallado. Devuelve el turno."""
        if verdict not in ("bien", "mal", ""):
            raise ValueError("el veredicto es 'bien', 'mal' o vacío para quitarlo")
        with self._lock, self._db:
            self._db.execute("UPDATE turns SET verdict = ?, note = ? WHERE id = ?", (verdict, note[:500], turn))
            row = self._db.execute("SELECT * FROM turns WHERE id = ?", (turn,)).fetchone()
        return dict(row) if row else None

    def rated(self, days: int = 7, verdict: str = "") -> list[dict]:
        """Los turnos valorados de los ultimos dias (para el repaso semanal)."""
        since = (datetime.now(self.tz) - timedelta(days=days)).isoformat()
        sql = "SELECT * FROM turns WHERE ts >= ? AND verdict != ''"
        args: tuple = (since,)
        if verdict:
            sql, args = sql + " AND verdict = ?", (since, verdict)
        with self._lock:
            return [dict(r) for r in self._db.execute(sql + " ORDER BY ts", args)]

    def since(self, days: int = 7) -> list[dict]:
        """Todos los turnos de los ultimos dias, para buscar lo que se repite."""
        start = (datetime.now(self.tz) - timedelta(days=days)).isoformat()
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM turns WHERE ts >= ? ORDER BY ts", (start,))]

    def day(self, day: date) -> list[tuple[str, str, str]]:
        """[(HH:MM, usuario, respuesta)] de ese dia."""
        start = datetime.combine(day, datetime.min.time(), self.tz)
        rows = self._db.execute(
            "SELECT ts, user, reply FROM turns WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start.isoformat(), (start + timedelta(days=1)).isoformat()),
        ).fetchall()
        return [(datetime.fromisoformat(r["ts"]).strftime("%H:%M"), r["user"], r["reply"]) for r in rows]

    def recent(self, session: str, limit: int = 6, hours: int = RECENT_HOURS) -> list[tuple[str, str]]:
        """[(usuario, respuesta)] de los ultimos turnos de esa sesion, del mas antiguo al mas reciente."""
        since = (datetime.now(self.tz) - timedelta(hours=hours)).isoformat()
        rows = self._db.execute(
            "SELECT user, reply FROM turns WHERE session = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
            (session, since, limit),
        ).fetchall()
        return [(r["user"], r["reply"]) for r in reversed(rows)]

    def related(self, words: list[str], limit: int = 5, skip: set[str] | None = None) -> list[tuple[str, str, str]]:
        """[(fecha, usuario, respuesta)] de conversaciones anteriores con palabras en comun (las mas parecidas y,
        a igualdad, las mas recientes). `skip`: frases del usuario que ya estan en el contexto."""
        from .memory.retrieval import keywords

        wanted = set(words)
        if not wanted:
            return []
        scored = []
        for row in self._db.execute("SELECT ts, user, reply FROM turns ORDER BY ts DESC LIMIT 2000"):
            ts, user, reply = row["ts"], row["user"], row["reply"]
            if skip and user in skip:
                continue
            said = set(keywords(user))
            hits = len(wanted & said) * 2 + len(wanted & set(keywords(reply)))
            # Al menos una palabra de lo que dijiste entonces, o dos de la respuesta.
            if (wanted & said and hits >= 2) or hits >= 3:
                scored.append((hits, ts, user, reply))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [(datetime.fromisoformat(ts).strftime("%d/%m %H:%M"), u, r) for _, ts, u, r in scored[:limit]]


def related_prompt(items: list[tuple[str, str, str]]) -> str:
    if not items:
        return ""
    lines = "\n".join(f"- [{when}] Usuario: {u[:300]} -> Tu: {r[:400]}" for when, u, r in items)
    return ("\n\nConversaciones anteriores relacionadas (tu registro; usalas si vienen a cuento, y si el usuario "
            "pregunta si te acuerdas de algo, contesta con esto. Para datos que cambian, como el tiempo, la agenda o "
            f"los precios, consulta siempre las herramientas en vez de fiarte de esto):\n{lines}")
