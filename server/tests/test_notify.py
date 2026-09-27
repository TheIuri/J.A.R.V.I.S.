import threading
import time as clock
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.main import create_app
from jarvis.notify import NoticeBoard, NtfyPush, in_quiet, parse_quiet
from jarvis.tools import ToolContext
from jarvis.tools.registry import ToolError
from jarvis.tools.reminders import ReminderStore, reminder_tools
from jarvis.watch import Watcher, calendar_check, reminders_check, truenas_check
from tests.test_calendar import cals
from tests.test_core import make_assistant

TZ = ZoneInfo("Europe/Madrid")


def test_quiet_hours_across_midnight():
    q = parse_quiet("23:00-08:00")
    assert in_quiet(time(23, 30), q) and in_quiet(time(7, 59), q) and not in_quiet(time(12, 0), q)
    assert in_quiet(time(14, 0), parse_quiet("13:00-15:00")) and not in_quiet(time(12, 0), None)
    with pytest.raises(ValueError):
        parse_quiet("de noche")


def test_board_dedups_persists_and_speaks(tmp_path):
    spoken, pushed = [], []
    seen = tmp_path / "seen.json"
    board = NoticeBoard(lambda t: spoken.append(t) or b"RIFF", push=lambda n, q: pushed.append((n.text, q)), seen_path=seen)
    n = board.post("info", "agenda", "En 15 minutos: Dentista.", key="cal:1")
    assert n.id == 1 and n.speak and n.audio_wav_b64 == "UklGRg=="
    assert board.post("info", "agenda", "otra vez", key="cal:1") is None  # repetido
    assert spoken == ["En 15 minutos: Dentista."] and pushed == [("En 15 minutos: Dentista.", False)]
    # Tras reiniciar, recuerda lo ya avisado.
    assert NoticeBoard(seen_path=seen).post("info", "agenda", "x", key="cal:1") is None


def test_board_quiet_hours_only_speak_critical():
    always_quiet = (time(0, 0), time(23, 59, 59))
    board = NoticeBoard(lambda t: b"RIFF", quiet=always_quiet)
    assert not board.post("warning", "TrueNAS", "disco caliente").speak
    assert board.post("critical", "TrueNAS", "pool degradado").speak


def test_board_long_poll():
    board = NoticeBoard()
    threading.Timer(0.1, lambda: board.post("info", "x", "hola")).start()
    start = clock.monotonic()
    assert [n.text for n in board.since(0, wait_s=5)] == ["hola"]
    assert clock.monotonic() - start < 2 and board.since(1, wait_s=0.05) == []


def test_ntfy_priority_and_quiet():
    sent = []

    def handler(request):
        sent.append({**dict(request.url.params), "authorization": request.headers.get("authorization")})
        return httpx.Response(200)

    push = NtfyPush("https://ntfy.sh/tema", "tok", client=httpx.Client(transport=httpx.MockTransport(handler)))
    board = NoticeBoard(push=push)
    board.post("critical", "TrueNAS", "pool degradado")
    push(board.post("warning", "TrueNAS", "caliente"), True)
    assert sent[0]["priority"] == "5" and sent[0]["authorization"] == "Bearer tok"
    assert sent[0]["title"] == "JARVIS · TrueNAS" and sent[2]["priority"] == "2"


def test_reminders_tool_and_due(tmp_path):
    store = ReminderStore(tmp_path / "r.db")
    set_, list_, cancel = reminder_tools(store)
    out = set_.fn(ToolContext(), text="llamar a mamá", minutes=30)
    assert out.startswith("Recordatorio 1 para hoy a las") or out.startswith("Recordatorio 1 para mañana")
    set_.fn(ToolContext(), text="sacar la basura", at="21:00", day="2030-01-02")
    assert "[2] el 02/01 a las 21:00: sacar la basura" in list_.fn(ToolContext())
    with pytest.raises(ToolError, match="ya ha pasado"):
        set_.fn(ToolContext(), text="x", at="10:00", day="2020-01-01")
    assert cancel.fn(ToolContext(), reminder_id=2) == "Recordatorio cancelado: sacar la basura"
    later = datetime.now(TZ) + timedelta(hours=1)
    assert [r.text for r in store.take_due(later)] == ["llamar a mamá"]
    assert store.take_due(later) == [] and list_.fn(ToolContext()) == "No hay recordatorios pendientes."


def test_watcher_reminders_and_calendar(tmp_path):
    store = ReminderStore(tmp_path / "r.db")
    store.add(datetime.now(TZ) + timedelta(seconds=1), "estirar")
    board = NoticeBoard()
    watcher = Watcher(board, [reminders_check(store)])
    clock.sleep(1.1)
    watcher.run_once()
    assert [n.text for n in board.since(0)] == ["Recordatorio: estirar."]

    c = cals()
    now = datetime.now(TZ)
    c.events = lambda start, days, only="": [  # cita dentro de 10 minutos y otra de todo el día
        type("E", (), {"all_day": False, "start": now + timedelta(minutes=10), "title": "Dentista",
                       "location": "Sabadell", "calendar": "personal"})(),
        type("E", (), {"all_day": True, "start": now + timedelta(minutes=5), "title": "Cumple",
                       "location": "", "calendar": "personal"})(),
    ]
    alerts = calendar_check(c, 15).fn()
    assert [a[3] for a in alerts] == ["En 10 minutos: Dentista en Sabadell."]


def test_truenas_watch_transitions():
    state = {"apps": [{"name": "plex", "state": "RUNNING"}], "alerts": []}

    class Client:
        def call(self, method, *a, **k):
            return {
                "pool.query": [{"name": "Data", "status": "ONLINE", "healthy": True}],
                "disk.temperatures": {"sda": 41, "sdb": 56},
                "app.query": state["apps"],
                "alert.list": state["alerts"],
                "replication.query": [{"id": 1, "name": "copia", "enabled": True, "state": {"state": "ERROR"}}],
                "cloudsync.query": [],
                "pool.snapshottask.query": [],
            }[method]

        def close(self):
            pass

    check = truenas_check(lambda: Client(), temp_warn=50)
    first = [a[3] for a in check.fn()]
    assert "El disco sdb está a 56 grados." in first and "La replicación copia ha fallado." in first
    assert not any("plex" in t for t in first)  # al arrancar no avisa del estado previo
    state["apps"] = [{"name": "plex", "state": "CRASHED"}]
    state["alerts"] = [{"uuid": "u1", "level": "CRITICAL", "formatted": "Pool Backup offline\ndetalles"}]
    second = check.fn()
    assert ("warning", "La app plex se ha caído.") in [(a[1], a[3]) for a in second]
    assert ("critical", "Alerta de TrueNAS: Pool Backup offline") in [(a[1], a[3]) for a in second]
    state["apps"] = [{"name": "plex", "state": "RUNNING"}]
    assert "La app plex vuelve a funcionar." in [a[3] for a in check.fn()]


def test_notifications_endpoint():
    assistant = make_assistant()
    board = NoticeBoard()
    assistant.board = board
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert client.get("/api/notifications").status_code == 401
    board.post("info", "x", "viejo")
    assert client.get("/api/notifications", headers=auth).json() == {"notices": [], "last": 1, "enabled": True}
    board.post("warning", "TrueNAS", "nuevo")
    body = client.get("/api/notifications?after=1", headers=auth).json()
    assert [n["text"] for n in body["notices"]] == ["nuevo"] and body["last"] == 2
    off = TestClient(create_app(make_assistant(), api_token="s")).get("/api/notifications", headers=auth).json()
    assert off["enabled"] is False
