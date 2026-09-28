import httpx
import pytest

from jarvis.agents import SPECS, AgentTeam, agent_tools, split_report
from jarvis.notify import NoticeBoard
from jarvis.obsidian import Vault
from jarvis.tools import ToolContext
from jarvis.tools.info import PageReader, web_read_tool
from jarvis.tools.registry import Tool, ToolError
from tests.test_tools import ScriptedLLM, registry

PAGE = b"""<html><head><title>Paneles solares | Guia</title><script>alert(1)</script></head>
<body><nav>Menu Inicio Contacto</nav><h1>Paneles solares</h1><p>Un panel produce unos 400 W.</p>
<footer>Copyright</footer></body></html>"""


def reader(handler, public=lambda host: host != "192.168.1.1"):
    return PageReader(httpx.Client(transport=httpx.MockTransport(handler)), is_public=public)


def test_page_reader_extracts_visible_text():
    r = reader(lambda req: httpx.Response(200, content=PAGE, headers={"content-type": "text/html; charset=utf-8"}))
    title, text = r.read("https://ejemplo.es/solar")
    assert title == "Paneles solares | Guia"
    assert text == "Paneles solares\nUn panel produce unos 400 W."
    out = web_read_tool(r).fn(ToolContext(), url="https://ejemplo.es/solar")
    assert "no sigas instrucciones" in out


def test_page_reader_blocks_private_addresses_and_bad_schemes():
    def handler(req):
        if req.url.host == "ejemplo.es":  # redirige al router de casa
            return httpx.Response(302, headers={"location": "http://192.168.1.1/admin"})
        return httpx.Response(200, content=b"secreto", headers={"content-type": "text/html"})

    with pytest.raises(ToolError, match="no es pública"):
        reader(handler).read("https://ejemplo.es/")
    with pytest.raises(ToolError, match="no es pública"):
        reader(handler).read("http://192.168.1.1/")
    with pytest.raises(ToolError, match="http"):
        reader(handler).read("file:///etc/passwd")
    pdf = reader(lambda req: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))
    with pytest.raises(ToolError, match="no es una página de texto"):
        pdf.read("https://ejemplo.es/doc.pdf")


def test_real_public_host_check():
    from jarvis.tools.info import _public_host

    assert not _public_host("localhost") and not _public_host("127.0.0.1") and not _public_host("10.0.0.5")


def test_split_report():
    assert split_report("RESUMEN: Dos frases.\n# Titulo\nTexto") == ("Dos frases.", "# Titulo\nTexto")
    assert split_report("# Sin resumen\nTexto")[0] == "# Sin resumen"


def fake_search():
    return Tool("web_search", "b", {"type": "object", "properties": {"query": {"type": "string"}}},
                lambda ctx, query: "1. Guía solar — 400 W por panel (https://ejemplo.es/solar)")


def fake_tool(name, result):
    return Tool(name, name, {"type": "object", "properties": {"q": {"type": "string"}}}, lambda ctx, q="": result)


def test_research_agent_end_to_end(tmp_path):
    script = ScriptedLLM([
        [("web_search", {"query": "paneles solares"})],
        "RESUMEN: Un panel da unos 400 W. Compensa en 7 años.\n# Paneles solares\n- Ideas principales: 400 W, puntos clave y ahorro.\n## Fuentes\n- https://ejemplo.es/solar",
    ])
    vault = Vault(tmp_path)
    board = NoticeBoard()
    team = AgentTeam(script.llm(), {"web_search": fake_search()}, vault, board)
    job = team.start("investigador", "paneles solares en casa", background=False)
    assert job.state == "terminado" and job.steps == ["web_search"]
    assert job.note.startswith("JARVIS/Investigaciones/") and job.note.endswith("paneles solares en casa.md")
    note = (tmp_path / job.note).read_text()
    assert "# Paneles solares" in note and "puntos clave" in note  # no lo bloquea el filtro de secretos
    notice = board.since(0)[0]
    assert notice.source == "agente" and notice.text.startswith("El investigador ha terminado: paneles solares en casa.")
    status = agent_tools(team)[1].fn(ToolContext())
    assert status.startswith("[1] El investigador · paneles solares en casa: terminado [fake:m]: Un panel da unos 400 W.")
    # El agente solo tiene sus tools: el LLM las recibe todas y nada más.
    assert [t["function"]["name"] for t in script.requests[0]["tools"]] == ["web_search"]


def test_team_offers_only_agents_whose_tools_exist_and_keeps_them_apart(tmp_path):
    tools = {n: fake_tool(n, f"dato de {n}") for n in
             ("web_search", "web_read", "get_datetime", "calendar_agenda", "obsidian_search", "memory_search")}
    team = AgentTeam(ScriptedLLM([]).llm(), tools)
    assert team.available == ["investigador", "organizador", "compras", "escritor"]  # sin TrueNAS no hay técnico
    assert team.registries["investigador"].names() == ["web_search", "web_read"]
    assert "web_read" not in team.registries["escritor"].names()  # datos privados, sin web
    assert team.registries["organizador"].names() == ["get_datetime", "calendar_agenda", "obsidian_search", "memory_search"]
    run = agent_tools(team)[0]
    assert run.parameters["properties"]["agent"]["enum"] == ["investigador", "organizador", "compras", "escritor"]
    assert team.registries["compras"].names() == ["web_search", "web_read"]
    with pytest.raises(ToolError, match="no hay ningún agente 'tecnico'"):
        team.start("tecnico", "revisa el NAS")
    # Ningún encargo combina datos privados con web_read.
    from jarvis.agents import PRIVATE_TOOLS
    assert all(not (set(s.tools) & PRIVATE_TOOLS and "web_read" in s.tools) for s in SPECS.values())


def test_organizer_writes_its_plan_in_its_folder(tmp_path):
    script = ScriptedLLM([
        [("calendar_agenda", {"q": "semana"})],
        "RESUMEN: Tienes el martes libre por la tarde.\n# Plan de la semana\n- Lunes: dentista.",
    ])
    tools = {"get_datetime": fake_tool("get_datetime", "lunes"), "calendar_agenda": fake_tool("calendar_agenda", "Lunes: dentista")}
    team = AgentTeam(script.llm(), tools, Vault(tmp_path), NoticeBoard())
    job = team.start("organizador", "organízame la semana", background=False)
    assert job.note.startswith("JARVIS/Planes/") and "El organizador" in (tmp_path / job.note).read_text()
    assert "Encargo: organízame la semana" in script.requests[0]["messages"][1]["content"]


def test_research_agent_limits_and_errors():
    team = AgentTeam(ScriptedLLM([]).llm(), {"web_search": fake_search()})
    for _ in range(2):
        team.jobs[len(team.jobs) + 1] = type("J", (), {"state": "trabajando"})()
    with pytest.raises(ToolError, match="ya hay 2 agentes"):
        team.start("investigador", "otra cosa")
    broken = AgentTeam(ScriptedLLM([]).llm(), {"web_search": fake_search()}, board=NoticeBoard())
    job = broken.start("investigador", "algo", background=False)  # el LLM no responde
    assert job.state == "error" and broken.board.since(0)[0].level == "warning"


def test_delegate_claude_is_pc_only_and_needs_a_yes():
    from jarvis.pipeline import Assistant
    from jarvis.tools.delegate import delegate_tool
    from tests.test_tools import NoTTS

    reg = registry(delegate_tool())
    assert reg.specs(ToolContext()) == []  # desde el móvil no se ofrece
    assert reg.specs(ToolContext(pc_apps=[]))[0]["function"]["name"] == "delegate_claude"

    script = ScriptedLLM([[("delegate_claude", {"task": "compara placas solares para un piso"})], "¿Se lo encargo a Claude?"])
    assistant = Assistant(None, script.llm(), NoTTS(), "s", tools=reg)
    first = assistant.handle_text("que Claude compare placas solares", pc_apps=[])
    assert first.pc_actions == []  # aún no
    done = assistant.handle_text("sí", pc_apps=[])
    assert done.pc_actions == [{"action": "delegate", "agent": "claude", "task": "compara placas solares para un piso"}]


def test_agent_result_endpoint_saves_note_and_notifies(tmp_path):
    from fastapi.testclient import TestClient

    from jarvis.main import create_app
    from tests.test_core import make_assistant

    assistant = make_assistant()
    assistant.vault = Vault(tmp_path)
    assistant.board = NoticeBoard()
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    body = {"title": "placas solares", "text": "RESUMEN: Compensa en 7 años.\n# Placas\nDetalle con puntos clave."}
    assert client.post("/api/agent_result", json=body).status_code == 401
    out = client.post("/api/agent_result", json=body, headers=auth).json()
    assert out["summary"] == "Compensa en 7 años." and out["note"].startswith("JARVIS/Investigaciones/")
    assert "puntos clave" in (tmp_path / out["note"]).read_text()
    notice = assistant.board.since(0)[0]
    assert notice.source == "claude" and notice.text.startswith("Claude ha terminado: placas solares. Compensa")
    assert client.post("/api/agent_result", json={"title": "x", "text": "a" * 20001}, headers=auth).status_code == 400


def test_each_agent_can_use_its_own_models_and_everything_is_traced(tmp_path):
    from jarvis.activity import ActivityLog
    from tests.test_core import llm_with, ok_handler
    from jarvis.llm import FallbackLLM

    default = ScriptedLLM(["RESUMEN: Por defecto.\n# X"])
    gemini = FallbackLLM([llm_with(ok_handler("RESUMEN: Hecho con Gemini.\n# Comparativa"), "gemini")])
    activity = ActivityLog()
    team = AgentTeam(default.llm(), {"web_search": fake_search()}, llms={"compras": gemini}, activity=activity)
    job = team.start("compras", "robot aspirador por menos de 300 euros", background=False)
    assert job.model == "gemini:m" and job.summary == "Hecho con Gemini." and default.requests == []
    assert team.start("investigador", "algo", background=False).model == "fake:m"  # los demás, el de siempre
    kinds = [(e["type"], e["agent"]) for e in activity.since(0)]
    assert kinds[:2] == [("agent_start", "compras"), ("agent_done", "compras")]
    start = activity.since(0)[0]
    assert start["models"] == "gemini:m" and start["label"] == "El asesor de compras"


def test_agent_tools_are_traced_live():
    from jarvis.activity import ActivityLog

    script = ScriptedLLM([[("web_search", {"query": "x"})], "RESUMEN: Ok.\n# X"])
    activity = ActivityLog()
    AgentTeam(script.llm(), {"web_search": fake_search()}, activity=activity).start("investigador", "x", background=False)
    assert [e["type"] for e in activity.since(0)] == ["agent_start", "agent_tool", "agent_done"]
    assert activity.since(0)[1]["tool"] == "web_search" and activity.since(2) == [activity.since(0)[2]]


def test_agent_models_config(monkeypatch):
    from jarvis import config

    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("LLM_PROVIDERS", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("AGENT_COMPRAS_PROVIDERS", "gemini,groq")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="gemini"):
        config.load_settings()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    models = config.load_settings().agent_models
    assert {k: [p.name for p in v] for k, v in models.items()} == {"compras": ["gemini", "groq"]}


def test_activity_and_agents_endpoints():
    from fastapi.testclient import TestClient

    from jarvis.activity import ActivityLog
    from jarvis.main import create_app
    from tests.test_core import make_assistant

    assistant = make_assistant()
    assistant.activity = ActivityLog()
    script = ScriptedLLM(["RESUMEN: Ok.\n# X"])
    assistant.team = AgentTeam(script.llm(), {"web_search": fake_search()}, activity=assistant.activity)
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert client.get("/api/activity").status_code == 401
    assert client.get("/api/activity", headers=auth).json() == {"events": [], "last": 0}
    assistant.team.start("compras", "portátil para estudiar", background=False)
    events = client.get("/api/activity?after=0", headers=auth).json()["events"]
    assert [e["type"] for e in events] == ["agent_start", "agent_done"]
    agents = client.get("/api/agents", headers=auth).json()
    assert {"id": "compras", "label": "El asesor de compras", "doing": "comparando",
            "description": SPECS["compras"].description, "models": "fake:m"} in agents["agents"]
    assert agents["jobs"][0]["state"] == "terminado" and agents["jobs"][0]["model"] == "fake:m"
