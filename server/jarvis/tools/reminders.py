"""Recordatorios: "recuerdame a las 18:00 llamar a mama".

Se guardan en SQLite (sobreviven a reinicios) y el vigilante los avisa a su hora por los HUD
y, si esta configurado, por notificacion push al movil.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .registry import Tool, ToolContext, ToolError

MAX_TEXT = 200
MAX_PENDING = 50


@dataclass(frozen=True)
class Reminder:
    id: int
    due: datetime
    text: str


class ReminderStore:
    def __init__(self, path: Path | str, timezone: str = "Europe/Madrid"):
        self.tz = ZoneInfo(timezone)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, due TEXT NOT NULL, text TEXT NOT NULL,"
                " done INTEGER NOT NULL DEFAULT 0)"
            )

    def add(self, due: datetime, text: str) -> Reminder:
        text = " ".join(text.split())
        if not text or len(text) > MAX_TEXT:
            raise ToolError(f"el recordatorio debe tener entre 1 y {MAX_TEXT} caracteres")
        if due <= datetime.now(self.tz):
            raise ToolError("esa hora ya ha pasado")
        with self._lock, self._db:
            if len(self.pending()) >= MAX_PENDING:
                raise ToolError("hay demasiados recordatorios pendientes")
            cur = self._db.execute("INSERT INTO reminders (due, text) VALUES (?, ?)", (due.isoformat(), text))
        return Reminder(cur.lastrowid, due, text)

    def pending(self) -> list[Reminder]:
        rows = self._db.execute("SELECT id, due, text FROM reminders WHERE done = 0 ORDER BY due").fetchall()
        return [Reminder(i, datetime.fromisoformat(d).astimezone(self.tz), t) for i, d, t in rows]

    def cancel(self, reminder_id: int) -> Reminder:
        with self._lock, self._db:
            match = next((r for r in self.pending() if r.id == reminder_id), None)
            if match is None:
                raise ToolError(f"no hay ningún recordatorio pendiente con id {reminder_id}")
            self._db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))
        return match

    def take_due(self, now: datetime | None = None) -> list[Reminder]:
        """Los que ya tocan; quedan marcados como hechos."""
        now = now or datetime.now(self.tz)
        with self._lock, self._db:
            due = [r for r in self.pending() if r.due <= now]
            self._db.executemany("UPDATE reminders SET done = 1 WHERE id = ?", [(r.id,) for r in due])
        return due


def when_text(due: datetime, now: datetime) -> str:
    day = due.date()
    if day == now.date():
        label = "hoy"
    elif day == now.date() + timedelta(days=1):
        label = "mañana"
    else:
        label = f"el {day:%d/%m}"
    return f"{label} a las {due:%H:%M}"


def reminders_card(ctx: ToolContext, store: ReminderStore, now: datetime, new_id: int | None = None) -> None:
    ctx.cards.append({"kind": "reminders", "items": [
        {"id": r.id, "when": when_text(r.due, now), "text": r.text, "new": r.id == new_id} for r in store.pending()[:12]]})


def reminder_tools(store: ReminderStore) -> list[Tool]:
    def set_(ctx: ToolContext, text: str, minutes: int | None = None, at: str = "", day: str = "") -> str:
        now = datetime.now(store.tz)
        if minutes is not None:
            due = now + timedelta(minutes=minutes)
        elif at:
            try:
                hour = time.fromisoformat(at.strip())
            except ValueError as exc:
                raise ToolError("'at' debe ser una hora HH:MM") from exc
            if day.strip().lower() in ("mañana", "manana"):
                target = now.date() + timedelta(days=1)
            elif day:
                try:
                    target = date.fromisoformat(day.strip())
                except ValueError as exc:
                    raise ToolError("'day' debe ser 'mañana' o una fecha AAAA-MM-DD") from exc
            else:
                target = now.date()
            due = datetime.combine(target, hour, store.tz)
            if not day and due <= now:
                due += timedelta(days=1)  # "a las 8" dicho a las 22:00 = mañana
        else:
            raise ToolError("indica 'minutes' o una hora 'at'")
        r = store.add(due.replace(second=0, microsecond=0), text)
        reminders_card(ctx, store, now, r.id)
        return f"Recordatorio {r.id} para {when_text(r.due, now)}: {r.text}"

    def list_(ctx: ToolContext) -> str:
        now = datetime.now(store.tz)
        pending = store.pending()
        reminders_card(ctx, store, now)
        if not pending:
            return "No hay recordatorios pendientes."
        return "\n".join(f"[{r.id}] {when_text(r.due, now)}: {r.text}" for r in pending)

    def cancel(_ctx: ToolContext, reminder_id: int) -> str:
        r = store.cancel(reminder_id)
        return f"Recordatorio cancelado: {r.text}"

    return [
        Tool(
            name="reminder_set",
            description=(
                "Crea un recordatorio que JARVIS avisará a su hora (en voz y en el móvil). Usa 'minutes' para "
                "'dentro de X minutos' o 'at' (HH:MM) y opcionalmente 'day' ('mañana' o AAAA-MM-DD)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Qué hay que recordar"},
                    "minutes": {"type": "integer", "minimum": 1, "maximum": 525600},
                    "at": {"type": "string", "description": "Hora HH:MM"},
                    "day": {"type": "string", "description": "'mañana' o AAAA-MM-DD"},
                },
                "required": ["text"],
            },
            fn=set_,
        ),
        Tool(
            name="reminder_list",
            description="Lista los recordatorios pendientes.",
            parameters={"type": "object", "properties": {}},
            fn=list_,
        ),
        Tool(
            name="reminder_cancel",
            description="Cancela un recordatorio pendiente por su id (míralo antes con reminder_list).",
            parameters={
                "type": "object",
                "properties": {"reminder_id": {"type": "integer", "minimum": 1}},
                "required": ["reminder_id"],
            },
            fn=cancel,
        ),
    ]
