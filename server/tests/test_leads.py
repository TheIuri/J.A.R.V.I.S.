import json
import time

import pytest

from jarvis.activity import ActivityLog
from jarvis.agents import PRIVATE_TOOLS, SPECS, AgentTeam
from jarvis.insights import Insights, parse_card
from jarvis.leads import LeadStore, extract_leads, lead_tools
from jarvis.obsidian import Vault
from jarvis.tools import ToolContext
from jarvis.tools.registry import ToolError
from tests.test_agents import fake_search
from tests.test_tools import ScriptedLLM

LEADS = [
    {"nombre": "Cafetería Luna", "tipo": "hostelería", "zona": "Sabadell", "web": "cafeterialuna.es",
     "contacto": "hola@cafeterialuna.es", "encaje": "Reforma y quiere decoración propia", "mensaje": "Hola, ..."},
    {"nombre": "Maquetas Pérez", "tipo": "arquitectura", "zona": "Terrassa", "web": "javascript:alert(1)",
     "contacto": "93 000 00 00", "encaje": "Maquetas", "mensaje": "Buenas, ..."},
    {"nombre": "", "web": "https://sin-nombre.es"},
]
REPORT = ("RESUMEN: He encontrado dos negocios que encajan. El mejor es la cafetería.\n# Clientes en Sabadell\n"
          "- Cafetería Luna: decoración impresa.\n## Fuentes\n- https://cafeterialuna.es\n"
          f"```leads\n{json.dumps(LEADS, ensure_ascii=False)}\n```")


def test_extract_leads_cleans_what_comes_from_the_web():
    leads, rest = extract_leads(REPORT)
    assert [lead["name"] for lead in leads] == ["Cafetería Luna", "Maquetas Pérez"]
    assert leads[0]["web"] == "https://cafeterialuna.es"
    assert leads[1]["web"] == ""  # nada de javascript: ni otros esquemas
    assert "```leads" not in rest and rest.endswith("https://cafeterialuna.es")
    assert extract_leads("sin bloque") == ([], "sin bloque")
    assert extract_leads("```leads\n[roto\n```")[0] == []


def test_lead_store_dedupes_updates_and_persists(tmp_path):
    store = LeadStore(tmp_path / "leads.json")
    leads, _ = extract_leads(REPORT)
    assert len(store.add(leads, "nota")) == 2
    again = [{**leads[0], "web": "https://www.cafeterialuna.es/"}, {**leads[1], "name": "maquetas perez"}]
    assert store.add(again) == []  # misma web o mismo nombre
    store.update("luna", "contactado", "le escribí el lunes")
    with pytest.raises(ToolError, match="estado no válido"):
        store.update(1, "borrado")
    reloaded = LeadStore(tmp_path / "leads.json")
    assert reloaded.find("#1")["status"] == "contactado" and reloaded.find(1)["note"] == "le escribí el lunes"
    assert reloaded.counts() == {"nuevo": 1, "contactado": 1, "interesado": 0, "descartado": 0}
    listing, update = lead_tools(reloaded)
    out = listing.fn(ToolContext(), status="nuevo")
    assert "#2 Maquetas Pérez" in out and "Luna" not in out
    assert "queda como descartado" in update.fn(ToolContext(), lead="maquetas", status="descartado")


def test_lead_hunter_saves_leads_and_traces_them(tmp_path):
    assert not (set(SPECS["captador"].tools) & PRIVATE_TOOLS)  # lee webs: nunca con datos privados
    script = ScriptedLLM([[("web_search", {"query": "cafeterías Sabadell"})], REPORT])
    activity = ActivityLog()
    store = LeadStore(tmp_path / "leads.json")
    team = AgentTeam(script.llm(), {"web_search": fake_search()}, Vault(tmp_path), activity=activity,
                     leads=store, lead_profile="Taller de impresión 3D en Badia del Vallès")
    job = team.start("captador", "clientes para decoración impresa en Sabadell", background=False)
    assert job.state == "terminado"
    assert "Lo que ofrece el usuario: Taller de impresión 3D" in script.requests[0]["messages"][1]["content"]
    note = (tmp_path / job.note).read_text()
    assert job.note.startswith("JARVIS/Leads/") and "```leads" not in note and "Cafetería Luna" in note
    assert [lead["name"] for lead in store.leads] == ["Cafetería Luna", "Maquetas Pérez"]
    assert store.leads[0]["source"] == job.note
    leads_event = [e for e in activity.since(0) if e["type"] == "leads"][0]
    assert leads_event["new"] == 2 and leads_event["names"] == ["Cafetería Luna", "Maquetas Pérez"]


def test_parse_card():
    card = parse_card('Aquí va: {"titulo": "Tiempo mañana", "datos": [{"k": "Máxima", "v": "22 °C"}, {"k": "", "v": "x"}]}')
    assert card == {"title": "Tiempo mañana", "items": [{"k": "Máxima", "v": "22 °C"}]}
    assert parse_card('{"datos": []}') is None and parse_card("nada") is None and parse_card("{roto") is None


def test_insights_only_for_turns_with_data_and_arrive_by_activity():
    activity = ActivityLog()
    script = ScriptedLLM(['{"titulo": "Tiempo", "datos": [{"k": "Máxima", "v": "22 °C"}]}'])
    ins = Insights(script.llm(), activity)
    assert not ins.submit("hola", "¡Hola!", [])  # nada que resumir: ni se pregunta al modelo
    assert ins.submit("qué tiempo hará", "Mañana 22 grados.", ["get_weather"])
    for _ in range(100):
        if ins.recent:
            break
        time.sleep(0.02)
    event = activity.since(0)[0]
    assert event["type"] == "insight" and event["title"] == "Tiempo" and event["items"] == [{"k": "Máxima", "v": "22 °C"}]
    assert event["tools"] == ["get_weather"] and len(script.requests) == 1


def test_pipeline_asks_for_insights_only_from_the_hud():
    from tests.test_core import make_assistant

    class Spy:
        def __init__(self):
            self.calls = []

        def submit(self, *args):
            self.calls.append(args)

    assistant = make_assistant()
    assistant.insights = Spy()
    assistant.handle_text("hola", session="briefing")  # turnos internos: sin ficha
    assert assistant.insights.calls == []
    assistant.handle_text("hola", on_event=lambda e: None)
    assert len(assistant.insights.calls) == 1


def test_leads_and_insights_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from jarvis.main import create_app
    from tests.test_core import make_assistant

    assistant = make_assistant()
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert client.get("/api/leads", headers=auth).status_code == 404  # sin agentes
    assert client.get("/api/insights", headers=auth).json() == {"insights": [], "enabled": False}
    assistant.leads = LeadStore(tmp_path / "leads.json")
    assistant.leads.add(extract_leads(REPORT)[0])
    assert client.get("/api/leads").status_code == 401
    out = client.get("/api/leads", headers=auth).json()
    assert [lead["name"] for lead in out["leads"]] == ["Maquetas Pérez", "Cafetería Luna"]  # lo último, primero
    assert out["counts"]["nuevo"] == 2
    done = client.post("/api/leads/update", json={"id": 1, "status": "interesado"}, headers=auth).json()
    assert done["lead"]["status"] == "interesado"
    assert client.post("/api/leads/update", json={"id": 1, "status": "x"}, headers=auth).status_code == 400


def test_lead_hunter_uses_the_profile_the_task_names(monkeypatch):
    from jarvis import config

    team = AgentTeam(ScriptedLLM([]).llm(), {"web_search": fake_search()}, lead_profile="Taller de impresión 3D",
                     lead_profiles={"caliperworks": "Software de gestión para talleres de impresión 3D"})
    assert team.lead_profile_for("busca clientes para Caliper Works en Cataluña") == (
        "caliperworks", "Software de gestión para talleres de impresión 3D")
    assert team.lead_profile_for("clientes para mi taller") == ("", "Taller de impresión 3D")
    assert "caliperworks" in __import__("jarvis.agents", fromlist=["agent_tools"]).agent_tools(team)[0].description
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("LEADS_PROFILE_CALIPERWORKS", "Software para talleres")
    assert config.load_settings().leads_profiles == {"caliperworks": "Software para talleres"}


def test_agents_endpoint_lists_the_hunter_profiles():
    from fastapi.testclient import TestClient

    from jarvis.main import create_app
    from tests.test_core import make_assistant

    assistant = make_assistant()
    assistant.team = AgentTeam(ScriptedLLM([]).llm(), {"web_search": fake_search()}, lead_profile="Taller",
                               lead_profiles={"caliperworks": "Software"})
    agents = TestClient(create_app(assistant, api_token="s")).get(
        "/api/agents", headers={"Authorization": "Bearer s"}).json()["agents"]
    hunter = next(a for a in agents if a["id"] == "captador")
    assert hunter["profiles"] == ["general", "caliperworks"]
    assert "profiles" not in next(a for a in agents if a["id"] == "compras")
