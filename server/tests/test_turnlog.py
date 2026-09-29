from jarvis.claude_code import ClaudeCode
from jarvis.memory.retrieval import keywords
from jarvis.pipeline import Assistant
from jarvis.turnlog import TurnLog
from tests.test_tools import NoTTS, ScriptedLLM


def test_recent_and_related(tmp_path):
    log = TurnLog(tmp_path / "t.db")
    log.add("hud", "crea un evento de dentista el jueves a las 10", "¿Creo el evento «Dentista» el jueves a las 10:00?")
    log.add("hud", "sí", "Hecho. Evento «Dentista» creado en Google Calendar.")
    log.add("otra", "pon música relajada", "Pongo música relajada en Spotify.")
    assert [u for u, _ in log.recent("hud")] == ["crea un evento de dentista el jueves a las 10", "sí"]
    found = log.related(keywords("¿te acuerdas del evento del dentista que te pedí?"))
    assert found and "dentista" in found[0][1]
    assert log.related(keywords("¿qué hora es?")) == []
    skipped = log.related(keywords("evento dentista"), skip={"crea un evento de dentista el jueves a las 10"})
    assert all("crea un evento" not in u for _, u, _ in skipped)


def test_conversation_survives_a_restart_and_remembers_older_talks(tmp_path):
    log = TurnLog(tmp_path / "t.db")
    log.add("hud", "apúntame el dentista el jueves", "Hecho. Evento «Dentista» creado en Google Calendar.")
    script = ScriptedLLM(["Sí, te lo creé el jueves.", "Vale."])
    a = Assistant(None, script.llm(), NoTTS(), "sistema")  # JARVIS recién reiniciado: historial vacío en RAM
    a.turn_log = log
    a.handle_text("¿te acuerdas del dentista?", session="hud")
    msgs = script.requests[0]["messages"]
    assert msgs[1] == {"role": "user", "content": "apúntame el dentista el jueves"}  # conversación retomada
    assert "Conversaciones anteriores" not in msgs[0]["content"]  # ya está en el historial: no se repite
    # "Nueva conversación": no se retoma, pero se recuerda como conversación anterior relacionada.
    a.reset("hud")
    script.steps.append("Sí.")
    a.handle_text("¿y lo del dentista?", session="hud")
    msgs = script.requests[1]["messages"]
    assert len(msgs) == 2 and "apúntame el dentista el jueves" in msgs[0]["content"]


def test_claude_sessions_are_kept_on_disk_and_new_ones_get_context(tmp_path):
    c = ClaudeCode("token", tmp_path, exe="claude")
    c.sessions["hud"] = "0b6c1c9e-1f1a-4d7e-9a55-3f2b8b0f5d11"
    c._save_sessions()
    assert ClaudeCode("token", tmp_path, exe="claude").sessions == {"hud": "0b6c1c9e-1f1a-4d7e-9a55-3f2b8b0f5d11"}
    c.reset("hud")
    assert ClaudeCode("token", tmp_path, exe="claude").sessions == {}

    log = TurnLog(tmp_path / "t.db")
    log.add("hud", "apúntame el dentista el jueves", "Hecho.")
    a = Assistant(None, None, NoTTS(), "sistema")
    a.turn_log = log
    ctx = a.claude_context("hud", "¿te acuerdas del dentista?")
    assert "Lo ultimo que hablasteis" in ctx and "apúntame el dentista" in ctx
    assert "Lo ultimo" not in a.claude_context("hud", "otra cosa")  # solo la primera vez
    assert "dentista" in ClaudeCode("t", tmp_path, exe="claude")._intro(ctx)


def test_long_claude_conversations_are_compacted(tmp_path):
    c = ClaudeCode("token", tmp_path, exe="claude")
    c.sessions["hud"] = "0b6c1c9e-1f1a-4d7e-9a55-3f2b8b0f5d11"
    c._maybe_compact("hud", 12_000)
    assert c.sessions and "hud" not in c.compacted
    c._maybe_compact("hud", c.compact_at + 1)
    assert "hud" not in c.sessions and "hud" in c.compacted
    assert ClaudeCode("token", tmp_path, exe="claude").sessions == {}  # tambien en disco

    log = TurnLog(tmp_path / "t.db")
    for i in range(12):
        log.add("hud", f"pregunta {i}", f"respuesta {i}")
    a = Assistant(None, None, NoTTS(), "sistema", history_turns=6)
    a.turn_log = log
    a._seeded.add("hud")  # ya estaba hablando: sin compactar no se repite nada
    assert "Lo ultimo" not in a.claude_context("hud", "hola")
    ctx = a.claude_context("hud", "hola", fresh=True)
    assert "compactado" in ctx and "pregunta 11" in ctx and "pregunta 2" in ctx and "pregunta 1 " not in ctx
