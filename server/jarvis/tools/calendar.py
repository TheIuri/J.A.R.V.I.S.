"""Agenda: lee calendarios de Google o de cualquiera que publique un enlace iCal (.ics).

Sin OAuth ni proyectos en la nube: cada servicio da una "direccion secreta en formato iCal".
Se configuran con CALENDARS ("personal=https://...ics;trabajo=https://...ics"). Esas URLs dan
acceso de lectura a tu agenda, asi que se tratan como una contrasena (solo en variables de entorno).
Solo lectura: crear eventos necesitaria OAuth (siguiente paso).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .basic import DAYS, MONTHS
from .registry import Tool, ToolContext, ToolError

CACHE_S = 300  # un calendario se descarga como mucho cada 5 minutos
MAX_EVENTS = 25


def parse_calendars(raw: str) -> dict[str, str]:
    calendars = {}
    for item in re.split(r";", raw or ""):
        if not item.strip():
            continue
        name, _, url = item.partition("=")
        name, url = name.strip().lower(), url.strip()
        if not name or not url.startswith(("https://", "webcal://")):
            raise ValueError("CALENDARS: usa nombre=https://...ics separados por ';'")
        calendars[name] = "https://" + url.removeprefix("webcal://") if url.startswith("webcal://") else url
    return calendars


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    title: str
    location: str
    calendar: str
    all_day: bool


class Calendars:
    def __init__(self, calendars: dict[str, str], timezone: str = "Europe/Madrid", client: httpx.Client | None = None):
        self.calendars = calendars
        self.tz = ZoneInfo(timezone)
        self._client = client or httpx.Client(timeout=15, follow_redirects=True)
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def _ics(self, name: str, url: str) -> bytes:
        with self._lock:
            cached = self._cache.get(name)
            if cached and time.monotonic() - cached[0] < CACHE_S:
                return cached[1]
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # No se incluye la URL: es secreta.
            raise ToolError(f"no puedo leer el calendario '{name}' ({type(exc).__name__})") from exc
        with self._lock:
            self._cache[name] = (time.monotonic(), resp.content)
        return resp.content

    def events(self, start: date, days: int, only: str = "") -> list[Event]:
        import icalendar
        import recurring_ical_events

        begin = datetime.combine(start, datetime.min.time(), self.tz)
        end = begin + timedelta(days=days)
        found: list[Event] = []
        for name, url in self.calendars.items():
            if only and name != only:
                continue
            try:
                cal = icalendar.Calendar.from_ical(self._ics(name, url))
                occurrences = recurring_ical_events.of(cal).between(begin, end)
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(f"el calendario '{name}' no es un iCal válido") from exc
            for ev in occurrences:
                if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                    continue
                s, e = ev.get("DTSTART").dt, (ev.get("DTEND") or ev.get("DTSTART")).dt
                all_day = not isinstance(s, datetime)
                found.append(
                    Event(
                        start=self._local(s),
                        end=self._local(e),
                        title=str(ev.get("SUMMARY", "(sin título)")).strip(),
                        location=str(ev.get("LOCATION", "")).strip(),
                        calendar=name,
                        all_day=all_day,
                    )
                )
        found.sort(key=lambda e: (e.start, not e.all_day, e.title))
        return found

    def _local(self, value) -> datetime:
        if not isinstance(value, datetime):  # evento de dia completo
            return datetime.combine(value, datetime.min.time(), self.tz)
        if value.tzinfo is None:  # hora "flotante": se entiende como local
            return value.replace(tzinfo=self.tz)
        return value.astimezone(self.tz)


def _day_label(d: date, today: date) -> str:
    if d == today:
        return "Hoy"
    if d == today + timedelta(days=1):
        return "Mañana"
    return f"{DAYS[d.weekday()].capitalize()} {d.day} de {MONTHS[d.month - 1]}"


def format_agenda(events: list[Event], start: date, days: int, today: date) -> str:
    if not events:
        span = _day_label(start, today).lower() if days == 1 else f"los próximos {days} días"
        return f"No hay nada en la agenda para {span}."
    lines, current = [], None
    for ev in events[:MAX_EVENTS]:
        day = ev.start.date()
        if day != current:
            current = day
            lines.append(f"{_day_label(day, today)}:")
        when = "todo el día" if ev.all_day else f"{ev.start:%H:%M}-{ev.end:%H:%M}"
        where = f" en {ev.location}" if ev.location else ""
        lines.append(f"- {when} {ev.title}{where} [{ev.calendar}]")
    if len(events) > MAX_EVENTS:
        lines.append(f"... y {len(events) - MAX_EVENTS} eventos más.")
    return "\n".join(lines)


def calendar_tool(calendars: Calendars) -> Tool:
    def run(_ctx: ToolContext, day: str = "hoy", days: int = 1, calendar: str = "") -> str:
        today = datetime.now(calendars.tz).date()
        key = day.strip().lower()
        if key in ("hoy", ""):
            start = today
        elif key in ("mañana", "manana"):
            start = today + timedelta(days=1)
        elif key in ("semana", "esta semana"):
            start, days = today, max(days, 7)
        else:
            try:
                start = date.fromisoformat(key)
            except ValueError as exc:
                raise ToolError("day debe ser 'hoy', 'mañana', 'semana' o una fecha AAAA-MM-DD") from exc
        return format_agenda(calendars.events(start, days, calendar.lower()), start, days, today)

    names = sorted(calendars.calendars)
    props = {
        "day": {"type": "string", "description": "'hoy', 'mañana', 'semana' o fecha AAAA-MM-DD"},
        "days": {"type": "integer", "minimum": 1, "maximum": 31, "description": "Cuántos días desde 'day'"},
    }
    if len(names) > 1:
        props["calendar"] = {"type": "string", "enum": names, "description": "Solo este calendario"}
    return Tool(
        name="calendar_agenda",
        description=f"Agenda del usuario (calendarios: {', '.join(names)}): citas y eventos de un día o varios.",
        parameters={"type": "object", "properties": props},
        fn=run,
        timeout_s=20,
    )
