"""Crear eventos en Google Calendar (lo de leer sigue en calendar.py, con iCal).

Necesita una app gratuita de Google y un inicio de sesion una sola vez en el PC (client/calendar_login.py),
que da un refresh token con permiso SOLO para eventos del calendario: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
GOOGLE_REFRESH_TOKEN (GOOGLE_CALENDAR_ID, por defecto "primary"). La app de Google debe estar "En produccion":
en "Prueba" el token caduca a los 7 dias.
Crear un evento siempre pide confirmacion ("si") antes de hacerlo.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .basic import DAYS, MONTHS
from .registry import Tool, ToolContext, ToolError

GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_API = "https://www.googleapis.com/calendar/v3"
MAX_TITLE = 200


class _OAuth:
    """Access token a partir del refresh token, cacheado hasta un minuto antes de caducar."""

    name = "?"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=15)
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def _refresh(self) -> dict[str, Any]:
        raise NotImplementedError

    def access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token[1]:
                return self._token[0]
            try:
                data = self._refresh()
            except httpx.HTTPError as exc:
                raise ToolError(f"no puedo conectar con {self.name} ({type(exc).__name__})") from exc
            self._token = (data["access_token"], time.monotonic() + int(data.get("expires_in", 3600)) - 60)
            return self._token[0]

    def _post_json(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(url, json=body, headers={"Authorization": f"Bearer {self.access_token()}"})
        except httpx.HTTPError as exc:
            raise ToolError(f"no puedo conectar con {self.name} ({type(exc).__name__})") from exc
        if resp.status_code in (401, 403):
            raise ToolError(f"{self.name} no me deja crear eventos; vuelve a ejecutar calendar_login.py")
        if resp.status_code >= 400:
            raise ToolError(f"{self.name} respondió {resp.status_code}")
        return resp.json()


class GoogleCalendar(_OAuth):
    name = "Google Calendar"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, calendar_id: str = "primary",
                 client: httpx.Client | None = None):
        super().__init__(client)
        self.creds = (client_id, client_secret, refresh_token)
        self.calendar_id = calendar_id or "primary"

    def _refresh(self) -> dict[str, Any]:
        client_id, secret, refresh = self.creds
        resp = self._client.post(GOOGLE_TOKEN, data={
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id, "client_secret": secret,
        })
        if resp.status_code != 200:
            raise ToolError("Google rechaza el acceso al calendario; vuelve a ejecutar calendar_login.py google")
        return resp.json()

    def create(self, title: str, start: datetime | date, end: datetime | date, tz: str, location: str = "",
               notes: str = "") -> str:
        if isinstance(start, datetime):
            when = {"start": {"dateTime": start.isoformat(), "timeZone": tz},
                    "end": {"dateTime": end.isoformat(), "timeZone": tz}}
        else:
            when = {"start": {"date": start.isoformat()}, "end": {"date": end.isoformat()}}
        body = {"summary": title, **when}
        if location:
            body["location"] = location
        if notes:
            body["description"] = notes
        url = f"{GOOGLE_API}/calendars/{quote(self.calendar_id, safe='')}/events"
        return self._post_json(url, body).get("htmlLink", "")


def _when(args: dict[str, Any], tz: ZoneInfo, now: datetime) -> tuple[datetime | date, datetime | date, str]:
    """(inicio, fin, texto para decir). Sin hora = todo el dia."""
    try:
        day = date.fromisoformat(str(args.get("date", "")).strip())
    except ValueError as exc:
        raise ToolError("la fecha debe ir como AAAA-MM-DD (mira get_datetime si hace falta)") from exc
    if day < now.date() - timedelta(days=1):
        raise ToolError("esa fecha ya ha pasado")
    if day > now.date() + timedelta(days=3 * 365):
        raise ToolError("esa fecha está demasiado lejos")
    label = f"el {DAYS[day.weekday()]} {day.day} de {MONTHS[day.month - 1]}"
    raw_time = str(args.get("time") or "").strip()
    if not raw_time:
        return day, day + timedelta(days=1), f"{label}, todo el día"
    try:
        hour, minute = (int(x) for x in raw_time.split(":"))
        start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    except ValueError as exc:
        raise ToolError("la hora debe ir como HH:MM") from exc
    minutes = int(args.get("duration_minutes") or 60)
    if not 5 <= minutes <= 24 * 60:
        raise ToolError("la duración va de 5 minutos a 24 horas")
    return start, start + timedelta(minutes=minutes), f"{label} a las {start:%H:%M}"


def calendar_add_tool(calendars: dict[str, GoogleCalendar], timezone: str = "Europe/Madrid",
                      now=None) -> Tool:
    tz = ZoneInfo(timezone)
    now = now or (lambda: datetime.now(tz))
    names = list(calendars)

    def pick(args: dict[str, Any]) -> str:
        return str(args.get("calendar") or names[0])

    def describe(args: dict[str, Any]) -> str:
        try:
            _, _, when = _when(args, tz, now())
        except ToolError:
            when = f"el {args.get('date')} {args.get('time') or ''}".strip()
        return f"crear en {calendars[pick(args)].name} el evento «{args.get('title')}» {when}"

    def run(_ctx: ToolContext, title: str, date: str, time: str = "", duration_minutes: int = 60,
            calendar: str = "", location: str = "", notes: str = "") -> str:
        title = " ".join(title.split())[:MAX_TITLE]
        if not title:
            raise ToolError("¿cómo se llama el evento?")
        args = {"date": date, "time": time, "duration_minutes": duration_minutes}
        start, end, when = _when(args, tz, now())
        cal = calendars[calendar or names[0]]
        cal.create(title, start, end, timezone, location.strip()[:200], notes.strip()[:1000])
        return f"Evento «{title}» creado en {cal.name} {when}."

    return Tool(
        name="calendar_add",
        description=(
            "Crea un evento en el calendario del usuario (" + ", ".join(names) + "). Necesita la fecha exacta "
            "(AAAA-MM-DD; usa get_datetime para saber qué día es hoy). Sin hora es de todo el día. "
            "Para avisos personales rápidos usa mejor reminder_set."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "description": "AAAA-MM-DD"},
                "time": {"type": "string", "description": "HH:MM de inicio; vacío = todo el día"},
                "duration_minutes": {"type": "integer", "description": "Por defecto 60"},
                "calendar": {"type": "string", "enum": names},
                "location": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["title", "date"],
        },
        fn=run,
        confirm=True,
        describe=describe,
        timeout_s=20,
    )


def calendar_missing_tool() -> Tool:
    """Sin Google Calendar conectado: la tool existe para que JARVIS diga la verdad en vez de inventarse que
    ha creado el evento (y ofrezca un recordatorio mientras tanto)."""

    def run(_ctx: ToolContext, title: str = "", date: str = "", **_: Any) -> str:
        raise ToolError(
            "todavía no puedo crear eventos: falta conectar Google Calendar (se hace una vez, ver "
            "'Crear eventos' en el README). Mientras tanto puedo ponerte un recordatorio"
        )

    return Tool(
        name="calendar_add",
        description="Crea un evento en el calendario del usuario (todavía sin conectar: explica cómo conectarlo).",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}, "date": {"type": "string", "description": "AAAA-MM-DD"}},
            "required": ["title"],
        },
        fn=run,
    )
