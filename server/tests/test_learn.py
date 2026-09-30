"""Aprender preferencias de lo que dice el usuario, sin gastar tokens en cada turno."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from jarvis.learn import PrefLearner, parse_prefs, worth_asking
from jarvis.memory import MemoryStore


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
        return Reply(self.answers.pop(0) if self.answers else '{"preferencias": []}')


@pytest.fixture()
def store():
    s = MemoryStore(":memory:")
    yield s
    s.close()


@pytest.mark.parametrize("text,ask", [
    ("Prefiero que me respondas corto", True),
    ("El filamento lo compro siempre en Filamentor", True),
    ("No me gustan los resúmenes largos", True),
    ("¿Qué tiempo hace mañana?", False),
    ("Pon música", False),
    ("Recuérdame llamar a Marta a las 18:00", False),
])
def test_filtro_previo(text, ask):
    assert worth_asking(text) is ask


def test_parse_prefs():
    assert parse_prefs('{"preferencias": ["Prefiere respuestas cortas", "x"]}') == ["Prefiere respuestas cortas"]
    assert parse_prefs("no es json") == []
    assert parse_prefs('{"preferencias": []}') == []


def test_guarda_la_preferencia(store):
    llm = FakeLLM('{"preferencias": ["Compra el filamento en Filamentor"]}')
    learner = PrefLearner(llm, store)
    saved = learner.learn("El filamento lo compro siempre en Filamentor")
    assert [s["content"] for s in saved] == ["Compra el filamento en Filamentor"]
    guardada = store.all("preferencia")[0]
    assert guardada.source == "aprendido" and guardada.confidence == 0.6


def test_no_repite_lo_que_ya_sabe(store):
    store.add("Compra el filamento en Filamentor", "preferencia")
    llm = FakeLLM('{"preferencias": ["Compra el filamento en Filamentor siempre"]}')
    assert PrefLearner(llm, store).learn("...") == []
    assert len(store.all("preferencia")) == 1


def test_no_guarda_secretos(store):
    llm = FakeLLM('{"preferencias": ["Su contraseña del banco es hunter2 y la usa siempre"]}')
    assert PrefLearner(llm, store).learn("...") == []
    assert store.all("preferencia") == []


def test_submit_no_pregunta_si_no_suena_a_preferencia(store):
    llm = FakeLLM()
    assert PrefLearner(llm, store).submit("¿qué hora es?") is False
    assert llm.calls == 0


def test_submit_pregunta_en_segundo_plano(store):
    llm = FakeLLM('{"preferencias": ["Prefiere respuestas cortas"]}')
    learner = PrefLearner(llm, store)
    assert learner.submit("Prefiero que me respondas corto") is True
    with learner._busy:  # espera a que el hilo termine
        assert [m.content for m in store.all("preferencia")] == ["Prefiere respuestas cortas"]
