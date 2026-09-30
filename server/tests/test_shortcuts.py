import httpx

from jarvis.pipeline import DIRECT, Assistant
from jarvis.shortcuts import direct_answer, groups_for, match, select_specs
from jarvis.tools import ToolContext
from jarvis.tools.basic import OpenMeteo, datetime_tool, weather_tool
from jarvis.tools.registry import Tool
from tests.test_tools import NoTTS, ScriptedLLM, registry


def spec(name):
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


ALL = [spec(n) for n in ["get_datetime", "get_weather", "calendar_agenda", "calendar_add", "spotify_play",
                         "spotify_control", "web_search", "memory_save", "truenas_status", "mi_tool_nueva"]]


def names(specs):
    return [s["function"]["name"] for s in specs]


def test_only_relevant_tools_are_sent():
    chosen, groups = select_specs(ALL, "¿Qué tiempo hará en Sabadell?")
    assert groups == {"tiempo"} and names(chosen) == ["get_datetime", "get_weather", "mi_tool_nueva"]
    chosen, _ = select_specs(ALL, "pon algo de Coldplay en Spotify")
    assert "spotify_play" in names(chosen) and "get_weather" not in names(chosen)
    # Nada claro: todas (nunca se queda sin la que necesita).
    assert select_specs(ALL, "hola, ¿cómo estás?")[0] == ALL
    # Seguimiento corto: se mantienen las del turno anterior.
    chosen, groups = select_specs(ALL, "¿y el jueves?", previous={"tiempo"})
    assert "get_weather" in names(chosen) and groups == {"tiempo"}
    assert groups_for("apúntame el dentista el jueves") >= {"agenda"}


def test_trivial_questions_are_recognised():
    assert match("¿Qué hora es?") == ("hora", {})
    assert match("Jarvis, ¿qué tiempo hará esta semana?") == ("tiempo", {"when": "esta semana"})
    assert match("¿Qué tengo mañana?") == ("agenda", {"when": "manana"})
    assert match("pon el volumen al 40") == ("volumen", {"vol": "40"})
    assert match("pausa") == ("pausa", {})
    for other in ["¿qué hora es en Tokio?", "¿qué tiempo hace en Londres?", "pon algo tranquilo", "apunta el dentista"]:
        assert match(other) is None, other


FORECAST = {
    "current": {"temperature_2m": 21.6, "apparent_temperature": 22.0, "weather_code": 2, "wind_speed_10m": 9.0},
    "daily": {"time": ["2026-09-29", "2026-09-30", "2026-10-01"], "weather_code": [2, 61, 0],
              "temperature_2m_min": [15.2, 14.1, 12.0], "temperature_2m_max": [24.6, 19.0, 23.0],
              "precipitation_probability_max": [10, 80, 0]},
}


def weather_registry():
    def handler(request):
        days = int(request.url.params["forecast_days"])
        return httpx.Response(200, json={"current": FORECAST["current"],
                                         "daily": {k: v[:days] for k, v in FORECAST["daily"].items()}})

    api = OpenMeteo(httpx.Client(transport=httpx.MockTransport(handler)))
    return registry(datetime_tool("Europe/Madrid"), weather_tool("", api, home_coords=(1, 2)))


def test_direct_answers_use_the_tools_data():
    reg = weather_registry()
    ctx = ToolContext()
    hit = direct_answer("¿Qué tiempo hace?", reg, ctx)
    assert hit.reply == "Ahora parcialmente nublado, 22 grados. Hoy entre 15 y 25." and ctx.cards[0]["kind"] == "weather"
    assert direct_answer("¿qué tiempo hará mañana?", reg, ToolContext()).reply == (
        "Mañana lluvia débil, entre 14 y 19 grados, con un 80 % de probabilidad de lluvia.")
    assert direct_answer("¿Qué hora es?", reg, ToolContext()).reply.startswith("Son las ")
    assert direct_answer("pausa", reg, ToolContext()) is None  # sin Spotify, al LLM


def test_assistant_answers_trivial_things_without_the_llm():
    script = ScriptedLLM([])  # si llamase al LLM, fallaría
    a = Assistant(None, script.llm(), NoTTS(), "s", tools=weather_registry())
    events = []
    result = a.handle_text("¿qué tiempo hace hoy?", on_event=events.append)
    assert result.provider == DIRECT and script.requests == [] and result.cards[0]["kind"] == "weather"
    assert any(e["type"] == "cards" for e in events) and result.reply.startswith("Ahora")
    # Lo que no es trivial va al LLM, con solo las herramientas que tocan.
    script.steps.append("Mañana llueve.")
    a.handle_text("¿crees que mañana necesitaré paraguas para ir a la oficina?")
    sent = [t["function"]["name"] for t in script.requests[0]["tools"]]
    assert sent == ["get_datetime", "get_weather"]


def test_filters_can_be_turned_off():
    script = ScriptedLLM(["Son las diez."])
    extra = Tool(name="spotify_play", description="x", parameters={"type": "object", "properties": {}},
                 fn=lambda _ctx: "ok")
    reg = weather_registry()
    reg.register(extra)
    a = Assistant(None, script.llm(), NoTTS(), "s", tools=reg)
    a.direct_answers = a.tool_filter = False
    a.handle_text("¿qué hora es?")
    assert len(script.requests) == 1 and len(script.requests[0]["tools"]) == 3
