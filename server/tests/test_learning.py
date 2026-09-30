"""Cómo aprende JARVIS: el pulgar, las correcciones, los atajos y el repaso semanal."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from jarvis.memory import MemoryStore
from jarvis.review import Proposals, Reviewer, apply_proposal, parse, week_facts
from jarvis.routes import Route, Routes, candidates, shape, speakable
from jarvis.tools import ToolContext
from jarvis.tools.registry import Tool, ToolRegistry
from jarvis.turnlog import TurnLog


@dataclass
class Reply:
    text: str
    provider: str = "test"


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def chat(self, messages, *a, **kw):
        self.calls += 1
        return Reply(self.answers.pop(0) if self.answers else "{}")


@pytest.fixture()
def log(tmp_path):
    return TurnLog(tmp_path / "t.db", "Europe/Madrid")


# --- el pulgar -------------------------------------------------------------------

def test_el_turno_guarda_con_que_se_contesto(log):
    turn = log.add("s", "¿qué tiempo hace?", "Hace sol.", "groq:gpt", ["get_weather"], 820)
    assert turn == 1
    row = log.rate(turn, "bien")
    assert row["model"] == "groq:gpt" and row["tools"] == "get_weather" and row["ms"] == 820
    assert row["verdict"] == "bien"


def test_pulgar_abajo_con_motivo(log):
    turn = log.add("s", "mi agenda", "No tienes nada.")
    log.rate(turn, "mal", "faltaba el calendario del trabajo")
    malos = log.rated(verdict="mal")
    assert len(malos) == 1 and malos[0]["note"] == "faltaba el calendario del trabajo"
    assert log.rated(verdict="bien") == []


def test_se_puede_quitar_el_pulgar(log):
    turn = log.add("s", "hola", "Hola.")
    log.rate(turn, "mal")
    log.rate(turn, "")
    assert log.rated() == []


def test_veredicto_invalido(log):
    turn = log.add("s", "hola", "Hola.")
    with pytest.raises(ValueError):
        log.rate(turn, "regular")


def test_el_registro_viejo_se_actualiza_sin_perder_nada(tmp_path):
    import sqlite3

    path = tmp_path / "viejo.db"
    old = sqlite3.connect(path)
    with old:
        old.execute("CREATE TABLE turns (ts TEXT NOT NULL, session TEXT, user TEXT, reply TEXT)")
        old.execute("INSERT INTO turns VALUES ('2026-09-01T10:00:00+02:00', 's', 'hola', 'Hola.')")
    old.close()
    log = TurnLog(path, "Europe/Madrid")  # abre el de antes y le añade las columnas nuevas
    assert log.add("s", "¿y mañana?", "Lloverá.", "groq", ["get_weather"], 100) == 1
    assert [t["user"] for t in log.since(3650)] == ["hola", "¿y mañana?"]


# --- correcciones ------------------------------------------------------------------

@pytest.mark.parametrize("text,es", [
    ("No, mira también el calendario del trabajo", True),
    ("te equivocas, eso fue el martes", True),
    ("Ya te dije que uso Filamentor", True),
    ("¿qué tiempo hace?", False),
    ("sí, perfecto", False),
])
def test_detecta_una_correccion(text, es):
    from jarvis.learn import looks_like_correction

    assert looks_like_correction(text) is es


def test_la_correccion_se_guarda_como_regla():
    from jarvis.learn import PrefLearner

    store = MemoryStore(":memory:")
    llm = FakeLLM('{"reglas": ["Cuando pregunte por la agenda, mira también el calendario del trabajo"]}')
    saved = PrefLearner(llm, store).learn_correction(
        "mi agenda", "No tienes nada.", "No, mira también el calendario del trabajo")
    assert len(saved) == 1
    regla = store.all("preferencia")[0]
    assert regla.source == "correccion" and regla.confidence == 0.8
    store.close()


def test_una_correccion_sin_regla_no_guarda_nada():
    from jarvis.learn import PrefLearner

    store = MemoryStore(":memory:")
    assert PrefLearner(FakeLLM('{"reglas": []}'), store).learn_correction("x", "y", "no, eran 3") == []
    assert store.all("preferencia") == []
    store.close()


# --- atajos aprendidos --------------------------------------------------------------

def test_shape_ignora_cortesias_y_numeros():
    assert shape("Jarvis, ¿cuánto queda del pedido 1234? por favor") == shape("cuanto queda del pedido 99")


@pytest.mark.parametrize("text,vale", [
    ("Quedan 3 días para que llegue el pedido.", True),
    ("ERROR: no puedo conectar", False),
    ("| A | B |\n|---|---|", False),
    ("Sí.", False),
    ('{"json": 1}', False),
])
def test_speakable(text, vale):
    assert speakable(text) is vale


def turnos(pregunta, tool, veces, verdict=""):
    return [{"user": pregunta, "tools": tool, "verdict": verdict} for _ in range(veces)]


def test_propone_lo_que_se_repite_con_la_misma_herramienta():
    found = candidates(turnos("cuanto queda del pedido", "shop_status", 4))
    assert len(found) == 1 and found[0].tool == "shop_status" and found[0].times == 4


def test_no_propone_lo_que_se_ha_preguntado_poco():
    assert candidates(turnos("cuanto queda del pedido", "shop_status", 3)) == []


def test_no_propone_si_alguna_vez_fallo():
    turns = turnos("cuanto queda del pedido", "shop_status", 4)
    turns[0]["verdict"] = "mal"
    assert candidates(turns) == []


def test_no_propone_herramientas_que_cambian_cosas():
    assert candidates(turnos("apunta la compra", "obsidian_daily_note", 6)) == []


def test_no_propone_lo_que_ya_contesta_un_atajo_de_serie():
    assert candidates(turnos("que hora es", "get_datetime", 8)) == []


def test_no_repite_un_atajo_que_ya_existe():
    known = {shape("cuanto queda del pedido"): Route(shape("cuanto queda del pedido"), "shop_status")}
    assert candidates(turnos("cuanto queda del pedido", "shop_status", 5), known) == []


def test_el_atajo_llama_a_la_herramienta_cada_vez(tmp_path):
    """Lo importante: guarda la ruta, no la respuesta. Si el dato cambia, la respuesta cambia."""
    valor = {"n": 3}
    registry = ToolRegistry()
    registry.register(Tool(name="shop_status", description="x", parameters={"type": "object", "properties": {}},
                           fn=lambda _ctx: f"Quedan {valor['n']} días para que llegue el pedido."))
    routes = Routes(tmp_path / "r.json")
    routes.add(Route(shape("cuanto queda del pedido"), "shop_status"))

    primera = routes.answer("¿Cuánto queda del pedido?", registry, ToolContext())
    assert primera == ("Quedan 3 días para que llegue el pedido.", "shop_status")
    valor["n"] = 1  # el dato cambia por su cuenta
    segunda = routes.answer("¿Cuánto queda del pedido?", registry, ToolContext())
    assert segunda[0] == "Quedan 1 días para que llegue el pedido."


def test_el_atajo_se_aparta_si_la_herramienta_deja_de_dar_una_frase(tmp_path):
    registry = ToolRegistry()
    registry.register(Tool(name="shop_status", description="x", parameters={"type": "object", "properties": {}},
                           fn=lambda _ctx: "ERROR: la tienda no responde"))
    routes = Routes(tmp_path / "r.json")
    routes.add(Route(shape("cuanto queda del pedido"), "shop_status"))
    assert routes.answer("cuanto queda del pedido", registry, ToolContext()) is None


def test_los_atajos_se_guardan_en_disco(tmp_path):
    routes = Routes(tmp_path / "r.json")
    routes.add(Route("cuanto queda del pedido", "shop_status", {}, 5, "2026-09-30T10:00"))
    assert Routes(tmp_path / "r.json").find("¿cuánto queda del pedido?").tool == "shop_status"
    assert routes.remove("cuanto queda del pedido") and not Routes(tmp_path / "r.json").items


# --- repaso semanal -------------------------------------------------------------------

def test_los_numeros_de_la_semana_se_cuentan_aqui():
    turns = ([{"user": "a", "reply": "x", "verdict": "bien", "tools": "get_weather", "model": "groq"}] * 3
             + [{"user": "b", "reply": "y", "verdict": "mal", "note": "falló", "tools": "", "model": "claude"}])
    facts = week_facts(turns)
    assert facts == facts | {"turnos": 4, "bien": 3, "mal": 1}
    assert facts["herramientas"][0] == ("get_weather", 3)
    assert facts["fallos"][0]["motivo"] == "falló"


def test_parse_del_repaso():
    summary, found = parse('{"resumen": "Bien.", "propuestas": [{"tipo": "regla", "titulo": "Mira el calendario"},'
                           ' {"tipo": "invento", "titulo": "no vale"}]}')
    assert summary == "Bien." and len(found) == 1 and found[0]["tipo"] == "regla"
    assert parse("sin json") == ("", [])


def test_el_repaso_propone_y_no_aplica_nada(tmp_path, log):
    for _ in range(4):
        log.add("s", "cuanto queda del pedido", "Quedan 3 días.", "groq", ["shop_status"], 100)
    malo = log.add("s", "mi agenda", "No tienes nada.")
    log.rate(malo, "mal", "faltaba el calendario del trabajo")

    store = MemoryStore(":memory:")
    proposals = Proposals(tmp_path / "p.json")
    llm = FakeLLM('{"resumen": "Una semana con un fallo.", "propuestas": ['
                  '{"tipo": "regla", "titulo": "Mirar el calendario del trabajo", "motivo": "falló el martes",'
                  ' "texto": "Cuando pregunte por la agenda, mira también el calendario del trabajo"}]}')
    out = Reviewer(llm, log, proposals, store).run({})

    tipos = sorted(p["kind"] for p in out["proposals"])
    assert tipos == ["atajo", "regla"]
    assert out["summary"] == "Una semana con un fallo."
    assert store.all("preferencia") == []  # nada aplicado todavía
    assert len(proposals.pending()) == 2
    store.close()


def test_aprobar_una_propuesta_la_aplica(tmp_path):
    store = MemoryStore(":memory:")
    routes = Routes(tmp_path / "r.json")
    proposals = Proposals(tmp_path / "p.json")

    atajo = proposals.add("atajo", "Contestar «cuanto queda» sin modelo", "4 veces",
                          {"shape": "cuanto queda del pedido", "tool": "shop_status", "times": 4})
    assert "Atajo activado" in apply_proposal(atajo, routes, store)
    assert routes.find("cuánto queda del pedido").tool == "shop_status"

    regla = proposals.add("regla", "Mirar el calendario del trabajo", "falló",
                          {"texto": "Cuando pregunte por la agenda, mira el calendario del trabajo"})
    assert "Regla guardada" in apply_proposal(regla, routes, store)
    guardada = store.all("preferencia")[0]
    assert guardada.source == "repaso" and "calendario" in guardada.content

    proposals.close(atajo.id, "aprobada")
    assert [p.id for p in proposals.pending()] == [regla.id]
    store.close()


def test_descartar_no_aplica_nada(tmp_path):
    proposals = Proposals(tmp_path / "p.json")
    item = proposals.add("regla", "Algo", "porque sí", {"texto": "x"})
    proposals.close(item.id, "descartada")
    assert proposals.pending() == []
    assert Proposals(tmp_path / "p.json").pending() == []  # y queda así en disco


def test_un_repaso_sin_datos_no_llama_al_modelo(tmp_path, log):
    llm = FakeLLM()
    out = Reviewer(llm, log, Proposals(tmp_path / "p.json")).run({})
    assert llm.calls == 0 and out["proposals"] == []


# --- buscar por significado en lo hablado ------------------------------------------

def test_el_indice_cubre_conversaciones_e_informes(tmp_path):
    from jarvis.notes_index import Doc, NotesIndex
    from jarvis.obsidian import Vault
    from jarvis.tools.obsidian import recall_tool

    (tmp_path / "Recetas").mkdir()
    (tmp_path / "Recetas" / "Lentejas.md").write_text("# Lentejas\nChorizo y patata.", encoding="utf-8")
    docs = [
        Doc("Conversación del 2026-09-28", "qué filamento compro",
            "¿qué filamento compro?\nEl PETG blanco a 6,50 € es el que mejor sale.", "conversacion"),
        Doc("Informe · precios de filamento", "El asesor", "El Nylon PA12 cuesta 46 €.", "informe"),
    ]
    index = NotesIndex(Vault(tmp_path), extra=lambda: docs)

    found = index.search("¿qué me dijiste del PETG?")
    assert found and found[0].kind == "conversacion" and "6,50" in found[0].snippet
    assert index.search_kind("nylon", ("informe",))[0].path.startswith("Informe")
    assert [f.kind for f in index.search("lentejas con chorizo")][0] == "nota"

    texto = recall_tool(index).fn(ToolContext(), query="qué me dijiste del PETG")
    assert "hablasteis" in texto and "6,50" in texto


def test_recall_sin_resultados(tmp_path):
    from jarvis.notes_index import NotesIndex
    from jarvis.obsidian import Vault
    from jarvis.tools.obsidian import recall_tool

    index = NotesIndex(Vault(tmp_path))
    assert "No encuentro nada" in recall_tool(index).fn(ToolContext(), query="cotización del bitcoin")


# --- el HUD habla con el servidor ----------------------------------------------------

def test_endpoints_de_aprendizaje(tmp_path):
    from fastapi.testclient import TestClient

    from jarvis.main import create_app
    from tests.test_core import make_assistant

    assistant = make_assistant()
    assistant.turn_log = TurnLog(tmp_path / "t.db", "Europe/Madrid")
    assistant.routes = Routes(tmp_path / "r.json")
    assistant.proposals = Proposals(tmp_path / "p.json")
    turn = assistant.turn_log.add("default", "mi agenda", "No tienes nada.", "groq", ["calendar_agenda"], 90)
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}

    assert client.post("/api/feedback", json={"turn": turn, "verdict": "bien"}).status_code == 401
    assert client.post("/api/feedback", json={"turn": turn, "verdict": "bien"}, headers=auth).json()["ok"]
    assert assistant.turn_log.rated(verdict="bien")[0]["id"] == turn
    assert client.post("/api/feedback", json={"turn": 999, "verdict": "mal"}, headers=auth).status_code == 404
    assert client.post("/api/feedback", json={"turn": turn, "verdict": "x"}, headers=auth).status_code == 400

    item = assistant.proposals.add("atajo", "Contestar «x» sin modelo", "4 veces",
                                   {"shape": "cuanto queda", "tool": "shop_status", "times": 4})
    listado = client.get("/api/proposals", headers=auth).json()
    assert [p["id"] for p in listado["proposals"]] == [item.id] and listado["routes"] == []

    out = client.post(f"/api/proposals/{item.id}", json={"approve": True}, headers=auth).json()
    assert "Atajo activado" in out["done"]
    assert client.get("/api/proposals", headers=auth).json()["routes"][0]["tool"] == "shop_status"
    assert client.post(f"/api/proposals/{item.id}", json={"approve": True}, headers=auth).status_code == 200
    assert client.delete("/api/routes/cuanto%20queda", headers=auth).status_code == 200
    assert client.get("/api/proposals", headers=auth).json()["routes"] == []
