import json

from jarvis.activity import ActivityLog
from jarvis.agents import AgentTeam
from jarvis.leads import LeadStore
from jarvis.obsidian import Vault
from jarvis.verify import Evidence, facts, verify
from tests.test_agents import fake_tool
from tests.test_tools import ScriptedLLM

SOURCE = ("Cafetería Luna (https://www.cafeterialuna.es/contacto) - escríbenos a hola@cafeterialuna.es o llama al "
          "+34 612 345 678. Impresora Bambu A1: 1.299,00 € en tienda.es")


def test_facts_and_evidence():
    found = facts("Web https://cafeterialuna.es, email hola@cafeterialuna.es, tel 612 34 56 78 y cuesta 1.299 €.")
    assert [k for k, _ in found] == ["web", "email", "teléfono", "precio"]
    ev = Evidence([SOURCE])
    assert ev.has_url("https://cafeterialuna.es") and ev.has_url("cafeterialuna.es/tienda")
    assert not ev.has_url("https://inventada.com")
    assert ev.has_email("HOLA@cafeterialuna.es") and not ev.has_email("info@cafeterialuna.es")
    assert ev.has_phone("612 34 56 78") and ev.has_phone("+34612345678") and not ev.has_phone("611 000 000")
    assert ev.has_price("1.299") and ev.has_price("1299,00") and not ev.has_price("1.199")


def test_verify_marks_what_no_source_backs():
    cards = [{"title": "A1", "price": "1.299 €", "url": "https://tienda.es/a1", "data": ""},
             {"title": "X1", "price": "999 €", "url": "https://otra.es/x1", "data": ""}]
    leads = [{"name": "Luna", "web": "https://cafeterialuna.es", "contact": "hola@cafeterialuna.es"},
             {"name": "Falsa", "web": "https://falsa.es", "contact": "info@falsa.es"}]
    result = verify("Mejor la A1 (1.299 €, https://tienda.es/a1). Llama al 699 999 999.", cards, leads, [SOURCE])
    assert cards[0]["unverified"] == [] and cards[1]["unverified"] == ["web: https://otra.es/x1", "precio: 999"]
    assert leads[0]["unverified"] == [] and len(leads[1]["unverified"]) == 2
    assert result["checked"] == 3 and result["unverified"] == ["teléfono: 699 999 999"]
    assert verify("x", [], [], [])["skipped"]  # sin fuentes no se juzga


def test_hunter_report_is_checked_before_saving(tmp_path):
    leads = [{"nombre": "Cafetería Luna", "web": "https://cafeterialuna.es", "contacto": "hola@cafeterialuna.es"},
             {"nombre": "Inventada", "web": "https://inventada.es", "contacto": "info@inventada.es"}]
    report = ("RESUMEN: Dos talleres encajan.\n# Clientes\n- Luna: https://cafeterialuna.es\n"
              f"```leads\n{json.dumps(leads)}\n```")
    script = ScriptedLLM([[("web_read", {"q": "cafeterialuna.es"})], report])
    activity = ActivityLog()
    store = LeadStore(tmp_path / "leads.json")
    (tmp_path / "v").mkdir()
    team = AgentTeam(script.llm(), {"web_search": fake_tool("web_search", ""), "web_read": fake_tool("web_read", SOURCE)},
                     Vault(tmp_path / "v"), activity=activity, leads=store, lead_profile="Software para talleres")
    job = team.start("captador", "clientes", background=False)
    assert job.state == "terminado"
    assert store.leads[0]["unverified"] == [] and len(store.leads[1]["unverified"]) == 2
    verify_event = next(e for e in activity.since(0) if e["type"] == "agent_verify")
    assert verify_event["unverified"] == []  # el informe solo cita lo que leyo
    assert "## Verificación de datos" in (tmp_path / "v" / job.note).read_text()
