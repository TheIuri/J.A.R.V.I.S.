import json

import pytest
from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.memory import MemoryRejected, MemoryStore, RuleRetriever, as_prompt, keywords, looks_secret
from jarvis.pipeline import Assistant
from jarvis.tools import ToolContext, build_registry

from test_tools import NoTTS, ScriptedLLM


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory.db")
    yield s
    s.close()


def test_crud_and_persistence(tmp_path):
    path = tmp_path / "m.db"
    s = MemoryStore(path)
    m = s.add("Su hija se llama Ana", "hecho")
    assert m.id == 1 and m.source == "usuario" and m.confidence == 1.0
    assert s.update(m.id, "Su hija se llama Ana María").content == "Su hija se llama Ana María"
    s.close()

    s = MemoryStore(path)  # sobrevive a un reinicio
    assert [x.content for x in s.all()] == ["Su hija se llama Ana María"]
    s.delete(1)
    assert s.all() == []
    with pytest.raises(MemoryRejected):
        s.delete(1)


def test_duplicates_are_not_stored_twice(store):
    a = store.add("Le gusta el café solo", "preferencia")
    b = store.add("  le gusta el café   solo ", "preferencia")
    assert a.id == b.id and len(store.all()) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Mi contraseña del wifi es patata123",
        "La API key es gsk_abcdefghijklmnop",
        "Mi tarjeta es 4111 1111 1111 1111",
        "Mi IBAN es ES91 2100 0418 4502 0005 1332",
    ],
)
def test_secrets_are_rejected(store, text):
    assert looks_secret(text)
    with pytest.raises(MemoryRejected, match="sensible"):
        store.add(text)
    assert store.all() == []


def test_invalid_type_and_empty(store):
    with pytest.raises(MemoryRejected):
        store.add("algo", "cotilleo")
    with pytest.raises(MemoryRejected):
        store.add("   ")


def test_search_ignores_accents_and_uses_prefixes(store):
    store.add("Trabaja en un proyecto de domótica con Home Assistant", "proyecto")
    store.add("Su perro se llama Toby", "hecho")
    assert [m.content for m in store.search(["domotica"])] == ["Trabaja en un proyecto de domótica con Home Assistant"]
    assert [m.content for m in store.search(["perr"])] == ["Su perro se llama Toby"]
    assert store.search(["inexistente"]) == []


def test_keywords_drop_stopwords_and_accents():
    assert keywords("¿Cómo se llama mi perro?") == ["llama", "perro"]


def test_retriever_always_includes_preferences_and_limits(store):
    store.add("Prefiere respuestas muy cortas", "preferencia")
    store.add("Su perro se llama Toby", "hecho")
    store.add("Vive en Badia del Vallès", "hecho")
    for i in range(10):
        store.add(f"Nota de perro número {i}", "evento")

    recalled = RuleRetriever(store, max_items=4).recall("¿cómo está mi perro?")
    assert len(recalled) == 4
    assert recalled[0].reason == "preferencia (siempre)"
    assert all("perro" in r.reason for r in recalled[1:])
    assert "Vive en Badia" not in as_prompt(recalled)
    assert "[1] (preferencia) Prefiere respuestas muy cortas" in as_prompt(recalled)
    assert as_prompt([]) == ""


def test_memory_tools_via_registry(store):
    reg = build_registry(Settings(api_token="t"), store)
    ctx = ToolContext()
    assert reg.execute("memory_save", json.dumps({"content": "Su color favorito es el verde", "type": "preferencia"}), ctx) == "Guardado como recuerdo 1."
    assert "[1] (preferencia)" in reg.execute("memory_search", '{"query": "color favorito"}', ctx)
    assert reg.execute("memory_update", '{"id": 1, "content": "Su color favorito es el azul"}', ctx) == "Recuerdo 1 actualizado."
    assert reg.execute("memory_save", '{"content": "mi password es 1234"}', ctx).startswith("ERROR: parece un dato sensible")
    assert reg.execute("memory_forget", '{"id": 1}', ctx) == "Olvidado: Su color favorito es el azul"
    assert reg.execute("memory_forget", '{"id": 1}', ctx).startswith("ERROR: no existe")
    assert "memory_save" not in build_registry(Settings(api_token="t")).names()


def test_recalled_memories_reach_the_llm_and_new_ones_are_saved(store):
    store.add("Su perro se llama Toby", "hecho")
    script = ScriptedLLM([[("memory_save", {"content": "Su gata se llama Luna", "type": "hecho"})], "Apuntado."])
    retriever = RuleRetriever(store)
    assistant = Assistant(None, script.llm(), NoTTS(), "SISTEMA", tools=build_registry(Settings(api_token="t"), store), memory=retriever)

    result = assistant.handle_text("Mi perro Toby ya tiene compañera: mi gata se llama Luna")
    system = script.requests[0]["messages"][0]["content"]
    assert system.startswith("SISTEMA") and "[1] (hecho) Su perro se llama Toby" in system
    assert "memory" in result.timings_ms
    assert [m.content for m in store.all()] == ["Su gata se llama Luna", "Su perro se llama Toby"]


def test_memory_api(store):
    store.add("Su perro se llama Toby", "hecho")
    assistant = Assistant(None, ScriptedLLM([]).llm(), NoTTS(), "s", memory=RuleRetriever(store))
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}

    assert client.get("/api/memories").status_code == 401
    listed = client.get("/api/memories", headers=auth).json()["memories"]
    assert [m["content"] for m in listed] == ["Su perro se llama Toby"]
    assert client.delete("/api/memories/1", headers=auth).json()["deleted"]["id"] == 1
    assert client.delete("/api/memories/1", headers=auth).status_code == 404

    no_memory = TestClient(create_app(Assistant(None, ScriptedLLM([]).llm(), NoTTS(), "s"), api_token="s"))
    assert no_memory.get("/api/memories", headers=auth).status_code == 404


def test_search_without_fts5_falls_back_to_like(store):
    store.add("Su perro se llama Toby", "hecho")
    store.add("Prefiere el té", "preferencia")
    store.fts = False
    assert [m.content for m in store.search(["perro"])] == ["Su perro se llama Toby"]
    assert store.search(["perro"], exclude_types=("hecho",)) == []


def test_recalled_memories_are_streamed_as_events(tmp_path):
    from jarvis.memory import MemoryStore, RuleRetriever
    from jarvis.pipeline import Assistant
    from tests.test_core import FakeTTS, make_assistant

    store = MemoryStore(tmp_path / "m.db")
    store.add("Prefiere respuestas cortas", "preferencia")
    base = make_assistant()
    assistant = Assistant(None, base.llm, FakeTTS(), "sistema", memory=RuleRetriever(store))
    events = []
    assistant.handle_text("hola", on_event=events.append)
    memory = next(e for e in events if e["type"] == "memory")
    assert memory["items"][0]["text"] == "Prefiere respuestas cortas"
    assert memory["items"][0]["reason"] == "preferencia (siempre)"
