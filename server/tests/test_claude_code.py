import json
import stat
import sys

from fastapi.testclient import TestClient

from jarvis.claude_code import ClaudeCode
from jarvis.main import create_app
from tests.test_core import make_assistant

FAKE = """#!{python}
import json, os, sys
prompt = sys.stdin.read()
with open(os.path.join(os.environ["HOME"], "call.json"), "w") as f:
    json.dump({{"argv": sys.argv[1:], "env": sorted(os.environ), "prompt": prompt,
               "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}}, f)
print(json.dumps({{"type": "system", "session_id": "abc12345-session"}}))
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {{"query": "tiempo Madrid"}}}}]}}}}))
print(json.dumps({{"type": "user", "message": {{"content": [
    {{"type": "tool_result", "tool_use_id": "t1", "content": "soleado"}}]}}}}))
print(json.dumps({{"type": "result", "result": "Hace sol en Madrid."}}))
"""


def fake_claude(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_claude_runs_with_only_web_tools_and_a_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "no-debe-llegar")
    home = tmp_path / "home"
    claude = ClaudeCode("tok-123", home, exe=fake_claude(tmp_path), memories=lambda: ["Se llama Ori"])
    events = []
    reply, used = claude.ask("¿qué tiempo hace?", "claude-sonnet", "s1", events.append)
    assert reply == "Hace sol en Madrid." and used == ["web_search"]
    assert [e["type"] for e in events] == ["tool", "tool_result"]
    call = json.loads((home / "call.json").read_text())
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "Bash" in argv[argv.index("--disallowedTools") + 1] and "--strict-mcp-config" in argv
    assert "¿qué tiempo hace?" not in " ".join(argv)  # lo del usuario va por stdin
    assert call["token"] == "tok-123" and "GROQ_API_KEY" not in call["env"] and "API_TOKEN" not in call["env"]
    assert "Se llama Ori" in call["prompt"]
    # Segunda pregunta: continua la misma conversacion.
    claude.ask("¿y mañana?", "claude-sonnet", "s1", events.append)
    call = json.loads((home / "call.json").read_text())
    assert call["argv"][-2:] == ["--resume", "abc12345-session"] and call["prompt"] == "¿y mañana?"


def test_claude_endpoint_only_when_configured(tmp_path):
    assistant = make_assistant()
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert "claude_models" not in client.get("/hud/config").json()
    assert client.post("/claude/chat/stream", json={"text": "hola", "model": "claude-sonnet"}, headers=auth).status_code == 404

    assistant.claude = ClaudeCode("tok", tmp_path / "home", exe=fake_claude(tmp_path))
    assert [m["id"] for m in client.get("/hud/config").json()["claude_models"]] == ["claude-sonnet", "claude-opus",
                                                                                     "claude-haiku"]
    assert client.post("/claude/chat/stream", json={"text": "hola", "model": "claude-sonnet"}).status_code == 401
    assert client.post("/claude/chat/stream", json={"text": "hola", "model": "gpt"}, headers=auth).status_code == 400
    resp = client.post("/claude/chat/stream", json={"text": "hola", "model": "claude-haiku"}, headers=auth)
    lines = [json.loads(line) for line in resp.text.splitlines()]
    assert lines[0]["type"] == "heard" and lines[-1]["type"] == "done"
    assert lines[-1]["reply"] == "Hace sol en Madrid." and lines[-1]["provider"] == "Claude Haiku"
    assert lines[-1]["audio_wav_b64"]  # la voz la pone JARVIS
    client.post("/api/reset", json={"session": "default"}, headers=auth)
    assert "default" not in assistant.claude.sessions
