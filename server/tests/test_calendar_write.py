import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from jarvis.config import Settings
from jarvis.pipeline import Assistant
from jarvis.tools import ToolContext, build_registry
from jarvis.tools.calendar_write import GoogleCalendar, OutlookCalendar, calendar_add_tool
from jarvis.tools.registry import ToolError
from tests.test_tools import NoTTS, ScriptedLLM, registry

TZ = ZoneInfo("Europe/Madrid")
NOW = lambda: datetime(2026, 9, 28, 12, 0, tzinfo=TZ)  # noqa: E731


class Recorder:
    def __init__(self, token_body=None):
        self.requests = []
        self.token_body = token_body or {"access_token": "AT", "expires_in": 3600}

    def __call__(self, request):
        self.requests.append(request)
        if "token" in request.url.path:
            return httpx.Response(200, json=self.token_body)
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"htmlLink": "https://calendar.google.com/e/1"})
        return httpx.Response(201, json={"webLink": "https://outlook.live.com/e/1"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


def test_google_creates_timed_and_all_day_events():
    rec = Recorder()
    cal = GoogleCalendar("id", "secret", "refresh", "familia@group.calendar.google.com", client=rec.client())
    tool = calendar_add_tool({"google": cal}, now=NOW)
    out = tool.fn(ToolContext(), title="Dentista", date="2026-09-30", time="10:30", duration_minutes=45)
    assert out == "Evento «Dentista» creado en Google Calendar el miércoles 30 de septiembre a las 10:30."
    token, event = rec.requests[0], rec.requests[1]
    assert b"grant_type=refresh_token" in token.content and b"client_secret=secret" in token.content
    assert event.url.raw_path.decode() == "/calendar/v3/calendars/familia%40group.calendar.google.com/events"
    assert event.headers["authorization"] == "Bearer AT"
    body = json.loads(event.content)
    assert body["start"] == {"dateTime": "2026-09-30T10:30:00+02:00", "timeZone": "Europe/Madrid"}
    assert body["end"]["dateTime"] == "2026-09-30T11:15:00+02:00"
    tool.fn(ToolContext(), title="Vacaciones", date="2026-10-12")
    assert json.loads(rec.requests[-1].content)["end"] == {"date": "2026-10-13"}
    assert sum("token" in r.url.path for r in rec.requests) == 1  # el access token se reutiliza


def test_bad_dates_are_rejected():
    tool = calendar_add_tool({"google": GoogleCalendar("i", "s", "r", client=Recorder().client())}, now=NOW)
    for kw, msg in [({"date": "30/09"}, "AAAA-MM-DD"), ({"date": "2026-01-01"}, "ya ha pasado"),
                    ({"date": "2026-10-01", "time": "diez"}, "HH:MM"),
                    ({"date": "2026-10-01", "time": "10:00", "duration_minutes": 2}, "duración")]:
        with pytest.raises(ToolError, match=msg):
            tool.fn(ToolContext(), title="x", **kw)


def test_outlook_keeps_the_rotated_refresh_token(tmp_path):
    token_file = tmp_path / "outlook_token.json"
    rec = Recorder({"access_token": "AT", "expires_in": 3600, "refresh_token": "NUEVO"})
    cal = OutlookCalendar("cid", "refresh-inicial", token_file=token_file, client=rec.client())
    tool = calendar_add_tool({"outlook": cal}, now=NOW)
    out = tool.fn(ToolContext(), title="Revisión coche", date="2026-10-02", time="09:00", location="Taller")
    assert "creado en Outlook el viernes 2 de octubre a las 09:00" in out
    assert b"refresh_token=refresh-inicial" in rec.requests[0].content
    assert rec.requests[0].url.path == "/common/oauth2/v2.0/token"
    body = json.loads(rec.requests[1].content)
    assert body["start"] == {"dateTime": "2026-10-02T09:00:00", "timeZone": "Europe/Madrid"}
    assert body["isAllDay"] is False and body["location"] == {"displayName": "Taller"}
    assert json.loads(token_file.read_text())["refresh_token"] == "NUEVO"
    # Tras reiniciar, usa el token renovado; si cambias la variable, manda la nueva.
    assert OutlookCalendar("cid", "refresh-inicial", token_file=token_file).refresh_token == "NUEVO"
    assert OutlookCalendar("cid", "otro-token-nuevo", token_file=token_file).refresh_token == "otro-token-nuevo"


def test_expired_login_says_how_to_fix_it():
    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    cal = GoogleCalendar("i", "s", "r", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="calendar_login.py google"):
        calendar_add_tool({"google": cal}, now=NOW).fn(ToolContext(), title="x", date="2026-10-01")


def test_creating_an_event_needs_a_yes():
    rec = Recorder()
    tool = calendar_add_tool({"google": GoogleCalendar("i", "s", "r", client=rec.client()),
                              "outlook": OutlookCalendar("c", "r", client=rec.client())}, now=NOW)
    assert tool.parameters["properties"]["calendar"]["enum"] == ["google", "outlook"]
    args = {"title": "Cena", "date": "2026-10-03", "time": "21:00", "calendar": "outlook"}
    assert tool.describe(args) == "crear en Outlook el evento «Cena» el sábado 3 de octubre a las 21:00"
    script = ScriptedLLM([[("calendar_add", args)], "¿Creo la cena el sábado a las 21:00?"])
    assistant = Assistant(None, script.llm(), NoTTS(), "s", tools=registry(tool))
    assistant.handle_text("apunta una cena el sábado a las nueve")
    assert rec.requests == []  # nada hasta el "sí"
    done = assistant.handle_text("sí")
    assert done.reply.startswith("Hecho. Evento «Cena» creado en Outlook") and len(rec.requests) == 2


def test_registry_offers_it_only_with_credentials(tmp_path):
    base = dict(api_token="t", data_dir=str(tmp_path))
    # Sin Google ni Outlook la tool existe, pero solo para decir la verdad: no crea nada y explica como conectarlo.
    missing = build_registry(Settings(**base))
    from jarvis.tools import ToolContext as _Ctx
    out = missing.execute("calendar_add", '{"title": "Dentista", "date": "2026-10-01"}', _Ctx())
    assert out.startswith("ERROR: todavía no puedo crear eventos") and "recordatorio" in out
    reg = build_registry(Settings(**base, outlook_client_id="c", outlook_refresh_token="r"))
    assert "calendar_add" in reg.names()
