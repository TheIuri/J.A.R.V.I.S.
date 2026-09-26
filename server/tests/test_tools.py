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
from jarvis.tools.truenas import summarize, truenas_tool


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
}


def test_truenas_summary():
    out = summarize("resumen", lambda method: FAKE_TRUENAS[method])
    assert "Sistema truenas (TrueNAS 26.0.0), encendido 1 d 1 h, carga 0.50." in out
    assert "Pool Data: ONLINE (sano), 75% usado, libres 1.024 GiB." in out
    assert "Apps en marcha (1): jarvis." in out
    assert "plex (stopped)" in out and "actualización disponible: plex" in out
    assert "Alerta WARNING: Pool Backup offline" in out and "vieja" not in out


def test_truenas_tool_closes_client_and_reports_connection_errors():
    closed = []

    class FakeClient:
        def call(self, method):
            return FAKE_TRUENAS[method]

        def close(self):
            closed.append(True)

    tool = truenas_tool("wss://nas/api/current", "jarvis", "k", False, connect=lambda *a: FakeClient())
    assert "Pool Data" in tool.fn(ToolContext(), section="discos")
    assert closed == [True]

    def refuse(*a):
        raise ConnectionRefusedError("refused")

    reg = registry(truenas_tool("wss://nas/api/current", "jarvis", "k", False, connect=refuse))
    assert "no puedo conectar con TrueNAS" in reg.execute("truenas_status", "{}", ToolContext())


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
