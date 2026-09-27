import json

import httpx
import pytest

from jarvis.tools import ToolContext
from jarvis.tools.homeassistant import HomeAssistant, ha_tools
from jarvis.tools.registry import ToolError
from jarvis.tools.spotify import Spotify, spotify_tools

CTX = ToolContext()

STATES = [
    {"entity_id": "light.salon", "state": "on", "attributes": {"friendly_name": "Luz del salón", "brightness": 128}},
    {"entity_id": "light.cocina", "state": "off", "attributes": {"friendly_name": "Luz cocina"}},
    {"entity_id": "climate.casa", "state": "heat",
     "attributes": {"friendly_name": "Termostato", "current_temperature": 20.5, "temperature": 21}},
    {"entity_id": "sensor.temp_exterior", "state": "14.2", "attributes": {"friendly_name": "Temperatura exterior",
                                                                          "unit_of_measurement": "°C"}},
    {"entity_id": "lock.puerta", "state": "locked", "attributes": {"friendly_name": "Puerta"}},
]


def home(calls, entities=""):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer tok"
        if request.method == "GET":
            return httpx.Response(200, json=STATES)
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[])

    return HomeAssistant("http://ha:8123/", "tok", entities, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_home_status_hides_locks_and_describes_devices():
    status, _ = ha_tools(home([]))
    out = status.fn(CTX)
    assert "Luz del salón: on, brillo 50%" in out and "Termostato: heat, 20.5 °C (objetivo 21 °C)" in out
    assert "Temperatura exterior: 14.2 °C" in out and "Puerta" not in out
    assert status.fn(CTX, query="temperatura exterior") == "Temperatura exterior: 14.2 °C"
    assert status.fn(CTX, query="luces").splitlines() == ["Luz del salón: on, brillo 50%", "Luz cocina: off"]


def test_home_control_by_name_with_limits():
    calls = []
    _, control = ha_tools(home(calls))
    assert control.fn(CTX, entity="luz del salon", action="apagar") == "Hecho: apagar Luz del salón."
    control.fn(CTX, entity="salón", action="brillo", value=30)
    control.fn(CTX, entity="termostato", action="temperatura", value=22.5)
    assert calls == [
        ("/api/services/light/turn_off", {"entity_id": "light.salon"}),
        ("/api/services/light/turn_on", {"entity_id": "light.salon", "brightness_pct": 30}),
        ("/api/services/climate/set_temperature", {"entity_id": "climate.casa", "temperature": 22.5}),
    ]
    with pytest.raises(ToolError, match="varias cosas"):
        control.fn(CTX, entity="luz", action="encender")
    with pytest.raises(ToolError, match="no encuentro"):
        control.fn(CTX, entity="puerta", action="abrir")  # cerraduras fuera
    with pytest.raises(ToolError, match="entre 5 y 35"):
        control.fn(CTX, entity="termostato", action="temperatura", value=80)


def test_home_entity_filter():
    status, _ = ha_tools(home([], entities="light."))
    assert "Termostato" not in status.fn(CTX) and "Luz cocina" in status.fn(CTX)


class FakeSpotify:
    def __init__(self, devices, search=None, status=204):
        self.devices, self.search, self.status = devices, search or {}, status
        self.calls = []

    def handler(self, request):
        path = request.url.path
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "A", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer A"
        if path == "/v1/me/player/devices":
            return httpx.Response(200, json={"devices": self.devices})
        if path == "/v1/search":
            return httpx.Response(200, json=self.search)
        self.calls.append((request.method, path, dict(request.url.params), request.content and json.loads(request.content)))
        return httpx.Response(self.status, json={"error": {"reason": "PREMIUM_REQUIRED"}} if self.status == 403 else None)

    def spotify(self, device=""):
        return Spotify("id", "secret", "refresh", device, client=httpx.Client(transport=httpx.MockTransport(self.handler)))


def test_spotify_play_picks_device_and_uses_context_for_artists():
    fake = FakeSpotify(
        devices=[{"id": "móvil", "name": "Pixel", "is_active": False}, {"id": "pc", "name": "SOBREMESA", "is_active": False}],
        search={"artists": {"items": [{"name": "Coldplay", "uri": "spotify:artist:1"}]},
                "tracks": {"items": [{"name": "Yellow", "uri": "spotify:track:9", "artists": [{"name": "Coldplay"}]}]}},
    )
    play, control, _ = spotify_tools(fake.spotify(device="sobremesa"))
    assert play.fn(CTX, query="coldplay", kind="artista") == "Sonando artista Coldplay."
    assert fake.calls[-1] == ("PUT", "/v1/me/player/play", {"device_id": "pc"}, {"context_uri": "spotify:artist:1"})
    assert play.fn(CTX, query="yellow", kind="cancion") == "Sonando cancion Yellow de Coldplay."
    assert fake.calls[-1][3] == {"uris": ["spotify:track:9"]}
    assert control.fn(CTX, action="volumen", value=40) == "Hecho: volumen 40%."
    assert fake.calls[-1][:3] == ("PUT", "/v1/me/player/volume", {"volume_percent": "40"})


def test_spotify_errors_are_explained():
    fake = FakeSpotify(devices=[{"id": "x", "name": "PC", "is_active": True}], status=403)
    with pytest.raises(ToolError, match="Premium"):
        fake.spotify().control("pausa")
    with pytest.raises(ToolError, match="ningún dispositivo"):
        FakeSpotify(devices=[]).spotify().control("reanudar")
    assert spotify_tools(FakeSpotify(devices=[]).spotify())[2].fn(CTX) == "No está sonando nada en Spotify."
