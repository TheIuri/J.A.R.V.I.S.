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
