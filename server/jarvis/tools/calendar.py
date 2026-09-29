"""Agenda: lee Google Calendar (con el mismo permiso que crea eventos) y cualquier calendario con enlace iCal.

- Google Calendar conectado (GOOGLE_CLIENT_ID/SECRET/REFRESH_TOKEN): se lee por su API, al momento. Es lo
  recomendable: el enlace iCal secreto de Google tarda horas en reflejar los eventos nuevos.
- CALENDARS ("personal=https://...ics;trabajo=https://...ics"): enlaces secretos iCal de solo lectura. Dan acceso
  a tu agenda, asi que se tratan como una contrasena (solo en variables de entorno).
Si un evento sale por los dos caminos (Google conectado y su iCal en CALENDARS), se muestra una sola vez.
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

CACHE_S = 300  # un calendario iCal se descarga como mucho cada 5 minutos
GOOGLE_CACHE_S = 60  # la API de Google se consulta como mucho una vez por minuto para el mismo rango
GOOGLE_NAME = "google"
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
    def __init__(self, calendars: dict[str, str], timezone: str = "Europe/Madrid", client: httpx.Client | None = None,
                 google=None):
        self.ics = calendars
        self.google = google  # GoogleCalendar (calendar_write) o None
        self.calendars = {**({GOOGLE_NAME: "api"} if google else {}), **calendars}
        self.tz = ZoneInfo(timezone)
        self._client = client or httpx.Client(timeout=15, follow_redirects=True)
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._google_cache: dict[tuple[str, str], tuple[float, list[Event]]] = {}
        self._lock = threading.Lock()

    def _google_events(self, begin: datetime, end: datetime) -> list[Event]:
        key = (begin.isoformat(), end.isoformat())
        with self._lock:
            cached = self._google_cache.get(key)
            if cached and time.monotonic() - cached[0] < GOOGLE_CACHE_S:
                return cached[1]
        found = []
        for item in self.google.list_events(begin, end):
            if item.get("status") == "cancelled":
                continue
            s, e = item.get("start") or {}, item.get("end") or {}
            try:
                if "dateTime" in s:
                    start = datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00"))
                    stop = datetime.fromisoformat((e.get("dateTime") or s["dateTime"]).replace("Z", "+00:00"))
                    all_day = False
                else:
                    start, stop, all_day = date.fromisoformat(s["date"]), date.fromisoformat(e.get("date", s["date"])), True
            except (KeyError, ValueError):
                continue
            found.append(Event(self._local(start), self._local(stop), str(item.get("summary") or "(sin título)").strip(),
                               str(item.get("location") or "").strip(), GOOGLE_NAME, all_day))
        with self._lock:
            self._google_cache[key] = (time.monotonic(), found)
        return found

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
        if self.google and only in ("", GOOGLE_NAME):
            found += self._google_events(begin, end)
        seen = {(e.start, e.title.lower()) for e in found}
        for name, url in self.ics.items():
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
                title = str(ev.get("SUMMARY", "(sin título)")).strip()
                if (self._local(s), title.lower()) in seen:
                    continue  # el mismo evento ya ha llegado por la API de Google
                found.append(
                    Event(
                        start=self._local(s),
                        end=self._local(e),
                        title=title,
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
    def run(ctx: ToolContext, day: str = "hoy", days: int = 1, calendar: str = "") -> str:
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
        events = calendars.events(start, days, calendar.lower())
        span = _day_label(start, today) if days == 1 else f"Próximos {days} días"
        ctx.cards.append({"kind": "agenda", "title": span, "events": [
            {"day": _day_label(e.start.date(), today), "date": e.start.date().isoformat(),
             "time": "" if e.all_day else f"{e.start:%H:%M}", "end": "" if e.all_day else f"{e.end:%H:%M}",
             "title": e.title[:160], "location": e.location[:160], "calendar": e.calendar}
            for e in events[:MAX_EVENTS]]})
        return format_agenda(events, start, days, today)

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


def calendar_agenda_missing_tool() -> Tool:
    """Sin calendario conectado: la tool existe para que JARVIS no diga "no tienes nada" sin haber mirado."""

    def run(_ctx: ToolContext, **_) -> str:
        raise ToolError("no tengo acceso a tu agenda: falta conectar Google Calendar (ver 'Crear eventos' en el "
                        "README) o poner enlaces iCal en CALENDARS. No puedo saber qué tienes")

    return Tool(
        name="calendar_agenda",
        description="Agenda del usuario (todavía sin conectar: dilo, no supongas que no hay nada).",
        parameters={"type": "object", "properties": {"day": {"type": "string"}}},
        fn=run,
    )
