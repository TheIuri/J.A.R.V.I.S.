import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.config import LLMProviderConfig, Settings
from jarvis.llm import FallbackLLM, OpenAICompatLLM
from jarvis.main import create_app
from jarvis.pipeline import Assistant
from jarvis.tools import ToolContext, ToolRegistry, build_registry
from jarvis.tools.basic import OpenMeteo, format_datetime, weather_tool
from jarvis.tools.pc import pc_tools
from jarvis.tools.registry import Tool
from jarvis.pipeline import is_affirmative
from jarvis.tools.truenas import summarize, truenas_tools


def echo_tool(**kw):
    return Tool(
        name="echo",
        description="eco",
        parameters={
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}, "mode": {"type": "string", "enum": ["a", "b"]}},
            "required": ["n"],
        },
        fn=lambda ctx, n, mode="a": f"{mode}{n}",
        **kw,
    )


def registry(*tools, disabled=None):
    reg = ToolRegistry(disabled=disabled)
    for t in tools:
        reg.register(t)
    return reg


# --- registro -----------------------------------------------------------------

def test_execute_validates_arguments():
    reg, ctx = registry(echo_tool()), ToolContext()
    assert reg.execute("echo", '{"n": 3, "mode": "b"}', ctx) == "b3"
    assert "obligatorio" in reg.execute("echo", "{}", ctx)
    assert "rango" in reg.execute("echo", '{"n": 9}', ctx)
    assert "uno de" in reg.execute("echo", '{"n": 1, "mode": "z"}', ctx)
    assert "tipo integer" in reg.execute("echo", '{"n": "1"}', ctx)
    assert "tipo integer" in reg.execute("echo", '{"n": true}', ctx)
    assert "desconocido" in reg.execute("echo", '{"n": 1, "x": 1}', ctx)
    assert "JSON" in reg.execute("echo", "no-json", ctx)
    assert "no existe" in reg.execute("rm_rf", "{}", ctx)


def test_disabled_and_destructive_tools_are_never_offered_or_run():
    reg = registry(echo_tool(), disabled={"echo"})
    assert reg.specs(ToolContext()) == []
    assert "no existe" in reg.execute("echo", '{"n": 1}', ToolContext())
    danger = registry(Tool("borrar", "x", {"type": "object", "properties": {}}, lambda ctx: "hecho", destructive=True))
    assert danger.specs(ToolContext()) == []
    assert danger.execute("borrar", "{}", ToolContext()).startswith("ERROR")


def test_tool_crash_and_timeout_become_errors():
    def boom(ctx):
        raise ValueError("x")

    def slow(ctx):
        time.sleep(1)
        return "tarde"

    empty = {"type": "object", "properties": {}}
    reg = registry(Tool("boom", "x", empty, boom), Tool("slow", "x", empty, slow, timeout_s=0.1))
    assert reg.execute("boom", "{}", ToolContext()) == "ERROR: fallo interno en la tool (ValueError)"
    assert "tiempo agotado" in reg.execute("slow", "{}", ToolContext())


def test_pc_tools_only_offered_to_pc_clients_with_app_allowlist():
    reg = registry(*pc_tools())
    assert reg.specs(ToolContext()) == []
    no_apps = {s["function"]["name"] for s in reg.specs(ToolContext(pc_apps=[]))}
    assert "pc_open_app" not in no_apps and "pc_volume" in no_apps
    ctx = ToolContext(pc_apps=["spotify", "calculadora"])
    specs = {s["function"]["name"]: s for s in reg.specs(ctx)}
    assert specs["pc_open_app"]["function"]["parameters"]["properties"]["app"]["enum"] == ["spotify", "calculadora"]
    assert reg.execute("pc_open_app", '{"app": "spotify"}', ctx) == "Acción enviada al PC."
    assert reg.execute("pc_open_app", '{"app": "cmd"}', ctx).startswith("ERROR: app no permitida")
    assert reg.execute("pc_open_url", '{"url": "file:///etc/passwd"}', ctx).startswith("ERROR")
    assert reg.execute("pc_volume", '{"action": "set"}', ctx).startswith("ERROR")
    reg.execute("pc_volume", '{"action": "set", "level": 40}', ctx)
    assert ctx.pc_actions == [{"action": "open_app", "app": "spotify"}, {"action": "volume", "mode": "set", "level": 40}]


# --- tools basicas --------------------------------------------------------------

def test_format_datetime_in_spanish():
    now = datetime(2026, 9, 26, 15, 7, tzinfo=ZoneInfo("Europe/Madrid"))
    assert format_datetime(now) == "sábado 26 de septiembre de 2026, 15:07 (CEST)"


def test_weather_uses_default_city_and_formats():
    def handler(request):
        if "geocoding" in request.url.host:
            assert request.url.params["name"] == "Valencia"
            return httpx.Response(200, json={"results": [{"name": "Valencia", "country": "España", "latitude": 39.47, "longitude": -0.38}]})
        return httpx.Response(200, json={
            "current": {"temperature_2m": 24.4, "apparent_temperature": 25.1, "weather_code": 1, "wind_speed_10m": 12.0},
            "daily": {"time": ["2026-09-26"], "weather_code": [61], "temperature_2m_min": [18.2],
                      "temperature_2m_max": [27.6], "precipitation_probability_max": [40]},
        })

    api = OpenMeteo(httpx.Client(transport=httpx.MockTransport(handler)))
    out = weather_tool("Valencia", api).fn(ToolContext())
    assert "Valencia (España) ahora: casi despejado, 24 °C" in out
    assert "Hoy: lluvia débil, 18–28 °C, 40% prob. de lluvia." in out


FORECAST = {
    "current": {"temperature_2m": 20.0, "apparent_temperature": 20.0, "weather_code": 0, "wind_speed_10m": 5.0},
    "daily": {"time": ["2026-09-26"], "weather_code": [0], "temperature_2m_min": [15.0],
              "temperature_2m_max": [25.0], "precipitation_probability_max": [0]},
}


def test_geocode_handles_region_suffix_and_missing_accents():
    searched = []

    def handler(request):
        if "geocoding" not in request.url.host:
            return httpx.Response(200, json=FORECAST)
        name = request.url.params["name"]
        searched.append(name)
        if name != "Badia":  # el buscador no entiende "Ciudad, Provincia" ni el nombre sin acento
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"results": [
            {"name": "Badia", "country": "Italia", "admin1": "Toscana", "latitude": 1, "longitude": 1},
            {"name": "Badia del Vallès", "country": "España", "admin2": "Barcelona", "latitude": 41.5, "longitude": 2.1},
        ]})

    api = OpenMeteo(httpx.Client(transport=httpx.MockTransport(handler)))
    out = weather_tool("Badia del Valles, Barcelona", api).fn(ToolContext())
    assert out.startswith("Badia del Vallès (España) ahora")
    assert searched == ["Badia del Valles, Barcelona", "Badia del Valles", "Badia"]


def test_home_coordinates_skip_geocoding():
    def handler(request):
        assert "geocoding" not in request.url.host, "no deberia buscar la ciudad"
        assert request.url.params["latitude"] == "41.508"
        return httpx.Response(200, json=FORECAST)

    api = OpenMeteo(httpx.Client(transport=httpx.MockTransport(handler)))
    tool = weather_tool("Badia del Vallès", api, home_coords=(41.508, 2.117))
    assert tool.fn(ToolContext()).startswith("Badia del Vallès ahora: despejado, 20 °C")
    assert tool.fn(ToolContext(), city="badia del valles").startswith("Badia del Vallès ahora")


def test_home_coordinates_config(monkeypatch):
    from jarvis import config

    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("HOME_LATITUDE", "41,508")
    monkeypatch.setenv("HOME_LONGITUDE", "2.117")
    assert config.load_settings().home_coords == (41.508, 2.117)
    monkeypatch.setenv("HOME_LONGITUDE", "este")
    with pytest.raises(RuntimeError, match="HOME_LATITUDE"):
        config.load_settings()


def test_weather_unknown_city_is_tool_error():
    api = OpenMeteo(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    reg = registry(weather_tool("", api))
    assert "no encuentro" in reg.execute("get_weather", '{"city": "Xyzzy"}', ToolContext())
    assert "HOME_CITY" in reg.execute("get_weather", "{}", ToolContext())


# --- TrueNAS --------------------------------------------------------------------

FAKE_TRUENAS = {
    "system.info": {"hostname": "truenas", "version": "26.0.0", "uptime_seconds": 90000, "loadavg": [0.5, 0.4, 0.3]},
    "pool.query": [{"name": "Data", "status": "ONLINE", "healthy": True, "size": 4 * 1024**4, "free": 1024**4}],
    "app.query": [
        {"name": "jarvis", "state": "RUNNING"},
        {"name": "plex", "state": "STOPPED", "upgrade_available": True},
    ],
    "alert.list": [{"level": "WARNING", "formatted": "Pool Backup offline", "dismissed": False},
                   {"level": "INFO", "formatted": "vieja", "dismissed": True}],
    "disk.temperatures": {"sda": 38, "sdb": {"temp": 52}, "nvme0n1": None},
    "pool.snapshottask.query": [
        {"dataset": "Data/fotos", "enabled": True, "state": {"state": "FINISHED", "datetime": {"$date": 1790000000000}}},
        {"dataset": "Data/viejo", "enabled": False, "state": {"state": "ERROR"}},
    ],
    "replication.query": [{"name": "a-backup", "enabled": True, "state": {"state": "ERROR", "error": "destino caido"}}],
    "cloudsync.query": [{"description": "B2", "enabled": True, "job": {"state": "SUCCESS", "time_finished": None}}],
}


def test_truenas_summary():
    out = summarize("resumen", lambda method: FAKE_TRUENAS[method])
    assert "Sistema truenas (TrueNAS 26.0.0), encendido 1 d 1 h, carga 0.50." in out
    assert "Pool Data: ONLINE (sano), 75% usado, libres 1.024 GiB." in out
    assert "Apps en marcha (1): jarvis." in out
    assert "plex (stopped)" in out and "actualización disponible: plex" in out
    assert "Alerta WARNING: Pool Backup offline" in out and "vieja" not in out
    assert "Discos entre 38 y 52 °C." in out and "a 52 °C" in out
    assert "Copias con fallos: Replicación a-backup (error)." in out


def test_truenas_temperatures_and_backups_detail():
    call = lambda method: FAKE_TRUENAS[method]  # noqa: E731
    assert summarize("temperaturas", call).startswith("Temperatura de los discos: sda 38 °C, sdb 52 °C.")
    copias = summarize("copias", call)
    assert "Snapshot Data/fotos: finished (" in copias and "viejo" not in copias
    assert "Replicación a-backup: error — destino caido." in copias
    assert "Cloud Sync B2: success." in copias


def test_truenas_tool_closes_client_and_reports_connection_errors():
    closed = []

    class FakeClient:
        def call(self, method, *args, **kw):
            return FAKE_TRUENAS[method]

        def close(self):
            closed.append(True)

    status, _restart = truenas_tools("wss://nas/api/current", "jarvis", "k", False, connect=lambda *a: FakeClient())
    assert "Pool Data" in status.fn(ToolContext(), section="discos")
    assert closed == [True]

    def refuse(*a):
        raise ConnectionRefusedError("refused")

    reg = registry(*truenas_tools("wss://nas/api/current", "jarvis", "k", False, connect=refuse))
    assert "no puedo conectar con TrueNAS" in reg.execute("truenas_status", "{}", ToolContext())


def test_affirmative_answers():
    for yes in ["Sí", "sí, hazlo", "Vale.", "adelante jarvis", "confirmo", "sí por favor"]:
        assert is_affirmative(yes), yes
    for other in ["no", "sí, pero reinicia plex", "espera", "vale no", "¿qué app?", "no, mejor no"]:
        assert not is_affirmative(other), other


def test_restart_needs_a_spoken_yes_in_the_next_turn():
    calls = []

    class FakeClient:
        def call(self, method, *args, **kw):
            calls.append((method, args, kw))
            return [{"name": "Plex", "state": "STOPPED"}] if method == "app.query" else None

        def close(self):
            pass

    tools = truenas_tools("wss://nas/api/current", "jarvis", "k", False, connect=lambda *a: FakeClient())
    script = ScriptedLLM([[("truenas_app_restart", {"app": "plex"})], "¿Confirmas que reinicie Plex?",
                          [("truenas_app_restart", {"app": "plex"})], "¿Lo reinicio?"])
    assistant = Assistant(None, script.llm(), NoTTS(), "sistema", tools=registry(*tools))

    # 1) El LLM propone: no se ejecuta nada, queda pendiente.
    events = []
    assert assistant.handle_text("reinicia plex", on_event=events.append).reply == "¿Confirmas que reinicie Plex?"
    assert calls == [] and "PENDIENTE DE CONFIRMACION" in script.requests[1]["messages"][-1]["content"]
    # 2) "sí" -> se ejecuta tal cual, sin volver a preguntar al LLM.
    done = assistant.handle_text("sí, hazlo")
    assert done.reply == "Hecho. La app Plex se ha reiniciado." and done.tools_used == ["truenas_app_restart"]
    assert ("app.redeploy", ("Plex",), {"job": True}) in calls and len(script.requests) == 2
    # 3) Otra propuesta y una respuesta que no es "sí": se cancela y sigue la conversación normal.
    calls.clear()
    assistant.handle_text("reinicia plex otra vez")
    assistant._pending["default"]  # sigue pendiente
    script.steps.append("Vale, no lo reinicio.")
    assert assistant.handle_text("no, déjalo").reply == "Vale, no lo reinicio."
    assert calls == [] and "default" not in assistant._pending
    # 4) Un "sí" sin nada pendiente no ejecuta nada.
    script.steps.append("¿Sí a qué?")
    assert assistant.handle_text("sí").reply == "¿Sí a qué?" and calls == []


def test_build_registry_requires_wss_for_truenas():
    base = dict(api_token="t", home_city="Madrid")
    assert build_registry(Settings(**base, tools_enabled=False)) is None
    names = build_registry(Settings(**base)).names()
    assert "get_weather" in names and "truenas_status" not in names
    with_nas = build_registry(Settings(**base, truenas_url="wss://nas/api/current", truenas_api_key="k"))
    assert "truenas_status" in with_nas.names()
    with pytest.raises(RuntimeError, match="wss://"):
        build_registry(Settings(**base, truenas_url="ws://nas/api/current", truenas_api_key="k"))


# --- bucle completo LLM + tools ------------------------------------------------

class ScriptedLLM:
    """Simula la API: primero pide tools, luego responde con texto."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.steps.pop(0)
        if isinstance(step, str):
            return httpx.Response(200, json={"choices": [{"message": {"content": step}}]})
        calls = [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(step)
        ]
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": calls}}]})

    def llm(self):
        cfg = LLMProviderConfig(name="fake", base_url="http://llm", api_key="k", model="m")
        client = httpx.Client(base_url="http://llm", transport=httpx.MockTransport(self.handler))
        return FallbackLLM([OpenAICompatLLM(cfg, 5, 100, client=client)])


class NoTTS:
    name = "none"

    def synthesize(self, text):
        return None


def test_tool_loop_executes_and_feeds_results_back():
    script = ScriptedLLM([[("echo", {"n": 2}), ("pc_volume", {"action": "mute"})], "Hecho, señor."])
    reg = registry(echo_tool(), *pc_tools())
    assistant = Assistant(None, script.llm(), NoTTS(), "sistema", tools=reg)
    result = assistant.handle_text("silencia y dime 2", pc_apps=[])

    assert result.reply == "Hecho, señor."
    assert result.tools_used == ["echo", "pc_volume"]
    assert result.pc_actions == [{"action": "volume", "mode": "mute", "level": None}]
    second = script.requests[1]["messages"]
    assert second[-2] == {"role": "tool", "tool_call_id": "c0", "content": "a2"}
    assert second[-1]["content"] == "Acción enviada al PC."
    # El historial solo guarda el texto final, no los mensajes de tools.
    assert [m["role"] for m in assistant._history["default"]] == ["user", "assistant"]


def test_tool_loop_is_bounded():
    script = ScriptedLLM([[("echo", {"n": 1})]] * 4 + ["Me rindo."])
    assistant = Assistant(None, script.llm(), NoTTS(), "sistema", tools=registry(echo_tool()))
    assert assistant.handle_text("bucle").reply == "Me rindo."
    assert "tools" not in script.requests[-1]


def test_api_chat_returns_pc_actions_and_speak_endpoint():
    script = ScriptedLLM([[("pc_media", {"action": "next"})], "Siguiente canción."])
    assistant = Assistant(None, script.llm(), NoTTS(), "sistema", tools=registry(*pc_tools()))
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}

    body = client.post("/api/chat", json={"text": "siguiente", "pc_apps": ["spotify"]}, headers=auth).json()
    assert body["pc_actions"] == [{"action": "media", "key": "next"}]
    assert body["tools_used"] == ["pc_media"]
    assert client.post("/api/speak", json={"text": "hola"}, headers=auth).status_code == 200
    assert client.post("/api/speak", json={"text": "hola"}).status_code == 401


# --- flujo de pensamiento en directo --------------------------------------------

def test_turn_emits_live_events_in_order():
    script = ScriptedLLM([[("echo", {"n": 2}), ("echo", {"n": 9})], "Listo."])
    assistant = Assistant(None, script.llm(), NoTTS(), "sistema", tools=registry(echo_tool()))
    events = []
    assistant.handle_text(" dime dos ", on_event=events.append)

    assert [e["type"] for e in events] == [
        "heard", "thinking", "tool", "tool_result", "tool", "tool_result", "thinking", "reply", "speaking",
    ]
    assert events[0]["text"] == "dime dos"
    assert events[2] == {"type": "tool", "id": "c0", "name": "echo", "args": {"n": 2}}
    assert events[3]["ok"] is True and events[3]["text"] == "a2"
    assert events[5]["ok"] is False and events[5]["text"].startswith("ERROR")  # 9 fuera de rango
    assert events[7]["text"] == "Listo."


def test_stream_endpoint_sends_ndjson_and_ends_with_done():
    script = ScriptedLLM([[("echo", {"n": 1})], "Hecho."])
    assistant = Assistant(None, script.llm(), NoTTS(), "sistema", tools=registry(echo_tool()))
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}

    assert client.post("/api/chat/stream", json={"text": "hola"}).status_code == 401
    resp = client.post("/api/chat/stream", json={"text": "hola"}, headers=auth)
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in resp.text.splitlines()]
    assert [e["type"] for e in events][-3:] == ["reply", "speaking", "done"]
    assert events[-1]["reply"] == "Hecho." and events[-1]["tools_used"] == ["echo"]


def test_stream_endpoint_reports_llm_errors_inside_the_stream():
    def down(request):
        return httpx.Response(503, json={"error": "caido"})

    cfg = LLMProviderConfig(name="fake", base_url="http://llm", api_key="k", model="m")
    llm = FallbackLLM([OpenAICompatLLM(cfg, 5, 100, client=httpx.Client(base_url="http://llm", transport=httpx.MockTransport(down)))])
    client = TestClient(create_app(Assistant(None, llm, NoTTS(), "sistema"), api_token="s"))
    resp = client.post("/api/chat/stream", json={"text": "hola"}, headers={"Authorization": "Bearer s"})
    last = json.loads(resp.text.splitlines()[-1])
    assert last["type"] == "error" and "503" in last["detail"]


# --- Wake-on-LAN -------------------------------------------------------------------

def test_wol_devices_and_packet():
    from jarvis.tools.wol import magic_packet, parse_devices

    assert parse_devices("Sobremesa=aa-bb-cc-dd-ee-ff; nas2=11:22:33:44:55:66") == {
        "sobremesa": "AA:BB:CC:DD:EE:FF", "nas2": "11:22:33:44:55:66"}
    with pytest.raises(ValueError, match="no válida"):
        parse_devices("pc=zz:zz")
    packet = magic_packet("AA:BB:CC:DD:EE:FF")
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6 and packet[6:12] == bytes.fromhex("AABBCCDDEEFF")


def test_wol_tool_sends_from_server_and_pc():
    from jarvis.tools.wol import wol_tool

    sent = []
    tool = wol_tool({"sobremesa": "AA:BB:CC:DD:EE:FF"}, "192.168.1.255", send=lambda mac, b: sent.append((mac, b)))
    reg = registry(tool)
    assert reg.specs(ToolContext())[0]["function"]["parameters"]["properties"]["device"]["enum"] == ["sobremesa"]
    ctx = ToolContext(pc_apps=[])
    out = reg.execute("wake_on_lan", '{"device": "sobremesa"}', ctx)
    assert "desde el servidor y el PC" in out and sent == [("AA:BB:CC:DD:EE:FF", "192.168.1.255")]
    assert ctx.pc_actions == [{"action": "wol", "mac": "AA:BB:CC:DD:EE:FF", "device": "sobremesa"}]
    assert "ERROR" in reg.execute("wake_on_lan", '{"device": "tostadora"}', ToolContext())


def test_cards_are_streamed_and_returned():
    def card_tool(ctx, n):
        ctx.cards.append({"kind": "web", "title": f"r{n}"})
        return "ok"

    tool = Tool("buscar", "b", {"type": "object", "properties": {"n": {"type": "integer"}}}, card_tool)
    script = ScriptedLLM([[("buscar", {"n": 1})], "Aquí tienes."])
    assistant = Assistant(None, script.llm(), NoTTS(), "sistema", tools=registry(tool))
    events = []
    result = assistant.handle_text("busca", on_event=events.append)
    assert {"type": "cards", "cards": [{"kind": "web", "title": "r1"}]} in events
    assert result.cards == [{"kind": "web", "title": "r1"}]


def test_music_always_goes_to_spotify_when_it_is_configured(monkeypatch):
    from jarvis import config
    from jarvis.prompts import system_prompt
    from jarvis.tools import build_registry

    assert "SIEMPRE con Spotify" in system_prompt("Jarvis", spotify=True)
    assert "teclas multimedia" in system_prompt("Jarvis") and "Spotify" not in system_prompt("Jarvis")
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    assert "pc_media" in build_registry(config.load_settings()).names()
    for var in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"):
        monkeypatch.setenv(var, "v")
    names = build_registry(config.load_settings()).names()
    assert "spotify_control" in names and "pc_media" not in names


def test_weather_for_the_whole_week():
    asked = []

    def handler(request):
        asked.append(request.url.params["forecast_days"])
        days = int(request.url.params["forecast_days"])
        return httpx.Response(200, json={
            "current": {"temperature_2m": 20.0, "apparent_temperature": 20.0, "weather_code": 0, "wind_speed_10m": 5.0},
            "daily": {"time": [f"2026-09-{28 + i:02d}" if 28 + i <= 30 else f"2026-10-{28 + i - 30:02d}" for i in range(days)],
                      "weather_code": [0] * days, "temperature_2m_min": [15.0] * days,
                      "temperature_2m_max": [25.0] * days, "precipitation_probability_max": [10] * days},
        })

    api = OpenMeteo(httpx.Client(transport=httpx.MockTransport(handler)))
    tool = weather_tool("", api, home_coords=(41.5, 2.1))
    out = tool.fn(ToolContext(), days=7)
    lines = out.splitlines()
    assert len(lines) == 8 and lines[1].startswith("Hoy:") and lines[2].startswith("Mañana:")
    assert lines[3].startswith("Miércoles 30:") and lines[4].startswith("Jueves 1:")
    tool.fn(ToolContext())
    tool.fn(ToolContext(), days=99)
    assert asked == ["7", "3", "14"]
    assert tool.parameters["properties"]["days"]["maximum"] == 14 and "esta semana" in tool.description


def test_weather_sends_an_exact_card_to_the_hud():
    def handler(request):
        return httpx.Response(200, json={
            "current": {"temperature_2m": 21.6, "apparent_temperature": 22.2, "weather_code": 2, "wind_speed_10m": 9.4,
                        "is_day": 0},
            "daily": {"time": ["2026-09-29", "2026-09-30"], "weather_code": [2, 61], "temperature_2m_min": [15.4, 14.6],
                      "temperature_2m_max": [24.5, 19.2], "precipitation_probability_max": [5, 80]},
        })

    ctx = ToolContext()
    weather_tool("", OpenMeteo(httpx.Client(transport=httpx.MockTransport(handler))), home_coords=(1, 2)).fn(ctx, days=2)
    card = ctx.cards[0]
    assert card["kind"] == "weather" and card["title"] == "casa"
    assert card["now"] == {"temp": 22, "feels": 22, "code": 2, "text": "parcialmente nublado", "wind": 9, "day": False}
    assert card["days"][1] == {"date": "2026-09-30", "label": "Mañana", "code": 61, "text": "lluvia débil",
                               "min": 15, "max": 19, "rain": 80}
