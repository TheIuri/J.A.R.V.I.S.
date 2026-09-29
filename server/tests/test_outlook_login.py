import json

import httpx
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.outlook_login import OutlookLogin
from jarvis.tools import calendar_writers
from tests.test_core import make_assistant


def microsoft(replies):
    """Microsoft simulado: /devicecode da el codigo y /token contesta lo que toque en cada consulta."""
    calls = []

    def handler(request):
        calls.append((request.url.path, dict(httpx.QueryParams(request.content.decode()))))
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "dev-1", "user_code": "ABCD-1234", "expires_in": 900,
                                             "interval": 1, "verification_uri": "https://microsoft.com/devicelogin"})
        return httpx.Response(200, json=replies.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_outlook_connects_with_a_code_and_saves_the_token(tmp_path):
    client, calls = microsoft([{"error": "authorization_pending"}, {"error": "slow_down"},
                               {"access_token": "a", "refresh_token": "rt-nuevo"}])
    connected = []
    login = OutlookLogin("cid", "common", tmp_path / "outlook_token.json", client=client, sleep=lambda s: None,
                         on_connected=lambda: connected.append(True))
    assert login.status() == {"state": "idle", "connected": False}
    out = login.start(background=False)
    assert out == {"state": "ok", "connected": True} and connected == [True]
    assert calls[0][1] == {"client_id": "cid", "scope": "Calendars.ReadWrite offline_access"}  # solo el calendario
    saved = json.loads((tmp_path / "outlook_token.json").read_text())
    assert saved == {"origin": "", "refresh_token": "rt-nuevo"}
    assert oct((tmp_path / "outlook_token.json").stat().st_mode)[-3:] == "600"

    # Con el token guardado, JARVIS ya tiene donde crear eventos (sin OUTLOOK_REFRESH_TOKEN).
    settings = Settings(api_token="t", data_dir=str(tmp_path), outlook_client_id="cid")
    writers = calendar_writers(settings)
    assert list(writers) == ["outlook"] and writers["outlook"].refresh_token == "rt-nuevo"


def test_outlook_login_errors_are_explained(tmp_path):
    client, _ = microsoft([{"error": "authorization_declined"}])
    login = OutlookLogin("cid", "common", tmp_path / "t.json", client=client, sleep=lambda s: None)
    assert login.start(background=False) == {"state": "error", "detail": "has rechazado el permiso", "connected": False}

    def refuse(request):
        return httpx.Response(400, json={"error_description": "AADSTS700016: app not found\nTrace ID: x"})

    bad = OutlookLogin("mal", "common", tmp_path / "t.json", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    out = bad.start(background=False)
    assert out["state"] == "error" and "OUTLOOK_CLIENT_ID" in out["detail"] and "AADSTS700016" in out["detail"]
    assert "Trace ID" not in out["detail"]


def test_calendar_endpoints(tmp_path):
    assistant = make_assistant()
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert client.get("/api/calendar/status", headers=auth).json() == {"google": False, "outlook": None}
    assert client.post("/api/calendar/outlook/connect", headers=auth).status_code == 404
    http, _ = microsoft([])
    assistant.outlook = OutlookLogin("cid", "common", tmp_path / "t.json", client=http, sleep=lambda s: None)
    assistant.outlook._wait = lambda *a: None  # que se quede esperando al usuario
    assert client.post("/api/calendar/outlook/connect").status_code == 401
    out = client.post("/api/calendar/outlook/connect", headers=auth).json()
    assert out["state"] == "pending" and out["user_code"] == "ABCD-1234" and "device_code" not in json.dumps(out)
    assert client.get("/api/calendar/status", headers=auth).json()["outlook"]["user_code"] == "ABCD-1234"
