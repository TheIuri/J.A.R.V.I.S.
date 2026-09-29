from datetime import date

import httpx
import pytest

from jarvis.tools import ToolContext
from jarvis.tools.calendar import Calendars, calendar_tool, format_agenda, parse_calendars
from jarvis.tools.registry import ToolError

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//ES
BEGIN:VEVENT
UID:1
DTSTART:20260928T070000Z
DTEND:20260928T080000Z
SUMMARY:Dentista
LOCATION:Sabadell
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;TZID=Europe/Madrid:20260901T183000
DTEND;TZID=Europe/Madrid:20260901T193000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Gimnasio
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART;VALUE=DATE:20260929
DTEND;VALUE=DATE:20260930
SUMMARY:Cumple de Ana
END:VEVENT
BEGIN:VEVENT
UID:4
DTSTART:20260928T100000Z
DTEND:20260928T110000Z
SUMMARY:Reunion anulada
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


def cals(hits=None):
    def handler(request):
        if hits is not None:
            hits.append(str(request.url))
        return httpx.Response(200, content=ICS)

    return Calendars({"personal": "https://cal/secret.ics"}, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_calendars():
    assert parse_calendars("Personal=https://a/b.ics; trabajo=webcal://c/d.ics") == {
        "personal": "https://a/b.ics", "trabajo": "https://c/d.ics"}
    with pytest.raises(ValueError):
        parse_calendars("x=http://inseguro.ics")


def test_events_expand_recurrences_and_skip_cancelled():
    hits = []
    c = cals(hits)
    events = c.events(date(2026, 9, 28), 2)
    assert [(e.start.strftime("%d %H:%M"), e.title, e.all_day) for e in events] == [
        ("28 09:00", "Dentista", False),  # 07:00 UTC = 09:00 en Madrid
        ("28 18:30", "Gimnasio", False),  # repetición semanal
        ("29 00:00", "Cumple de Ana", True),
    ]
    c.events(date(2026, 9, 28), 1)
    assert len(hits) == 1  # caché


def test_agenda_text():
    c = cals()
    out = format_agenda(c.events(date(2026, 9, 28), 2), date(2026, 9, 28), 2, today=date(2026, 9, 27))
    assert out.splitlines() == [
        "Mañana:",
        "- 09:00-10:00 Dentista en Sabadell [personal]",
        "- 18:30-19:30 Gimnasio [personal]",
        "Martes 29 de septiembre:",
        "- todo el día Cumple de Ana [personal]",
    ]
    tool = calendar_tool(c)
    assert tool.fn(ToolContext(), day="2030-10-07").endswith("18:30-19:30 Gimnasio [personal]")  # sigue repitiéndose
    assert "No hay nada" in tool.fn(ToolContext(), day="2030-10-08")
    with pytest.raises(ToolError, match="AAAA-MM-DD"):
        tool.fn(ToolContext(), day="el finde")


def test_calendar_errors_do_not_leak_the_secret_url():
    c = Calendars({"trabajo": "https://calendario/SECRETO.ics"},
                  client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))))
    with pytest.raises(ToolError) as err:
        c.events(date(2026, 9, 28), 1)
    assert "SECRETO" not in str(err.value) and "trabajo" in str(err.value)


def google_api(items, calls):
    """Google Calendar simulado: token y lista de eventos."""
    from jarvis.tools.calendar_write import GoogleCalendar

    def handler(request):
        calls.append(request)
        if "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return httpx.Response(200, json={"items": items})

    return GoogleCalendar("id", "secret", "refresh", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_google_agenda_is_read_from_the_api_and_merged_with_ical():
    calls = []
    google = google_api([
        {"summary": "Revisión coche", "start": {"dateTime": "2026-09-29T10:00:00+02:00"},
         "end": {"dateTime": "2026-09-29T11:00:00+02:00"}, "location": "Taller"},
        {"summary": "Cumple de Ana", "start": {"date": "2026-09-29"}, "end": {"date": "2026-09-30"}},  # también en iCal
        {"summary": "Anulado", "status": "cancelled", "start": {"dateTime": "2026-09-29T12:00:00Z"}, "end": {}},
    ], calls)
    ics = cals()
    c = Calendars(ics.ics, client=ics._client, google=google)
    assert sorted(c.calendars) == ["google", "personal"]
    events = c.events(date(2026, 9, 29), 1)
    assert [(e.start.strftime("%d %H:%M"), e.title, e.calendar, e.all_day) for e in events] == [
        ("29 00:00", "Cumple de Ana", "google", True),  # una sola vez aunque esté en los dos
        ("29 10:00", "Revisión coche", "google", False),
    ]
    listed = [r for r in calls if "events" in r.url.path][0]
    assert listed.headers["authorization"] == "Bearer AT"
    assert listed.url.params["singleEvents"] == "true" and listed.url.params["timeMin"].startswith("2026-09-29T00:00")
    out = calendar_tool(c).fn(ToolContext(), day="2026-09-29")
    assert "10:00-11:00 Revisión coche en Taller [google]" in out
    assert [e.title for e in c.events(date(2026, 9, 29), 1, "personal")] == ["Cumple de Ana"]


def test_google_agenda_errors_are_explained():
    from jarvis.tools.calendar_write import GoogleCalendar

    def handler(request):
        if "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        return httpx.Response(403)

    google = GoogleCalendar("i", "s", "r", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="calendar_login.py google"):
        Calendars({}, google=google).events(date(2026, 9, 29), 1)


def test_agenda_without_calendars_says_so(tmp_path):
    from jarvis.config import Settings
    from jarvis.tools import build_registry, make_calendars

    base = dict(api_token="t", data_dir=str(tmp_path))
    assert make_calendars(Settings(**base)) is None
    out = build_registry(Settings(**base)).execute("calendar_agenda", '{"day": "mañana"}', ToolContext())
    assert out.startswith("ERROR: no tengo acceso a tu agenda")
    google_only = make_calendars(Settings(**base, google_client_id="c", google_client_secret="s", google_refresh_token="r"))
    assert list(google_only.calendars) == ["google"]
