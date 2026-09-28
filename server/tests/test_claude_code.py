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
    json.dump({{"argv": sys.argv[1:], "env": sorted(os.environ), "prompt": prompt, "cwd": os.getcwd(),
               "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")}}, f)
print(json.dumps({{"type": "system", "session_id": "abc12345-session"}}))
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "id": "t1", "name": "WebSearch", "input": {{"query": "tiempo Madrid"}}}}]}}}}))
print(json.dumps({{"type": "user", "message": {{"content": [
    {{"type": "tool_result", "tool_use_id": "t1", "content": "soleado"}}]}}}}))
{extra}
print(json.dumps({{"type": "result", "result": "Hace sol en Madrid.", "duration_ms": 1200,
                  "usage": {{"input_tokens": 30, "output_tokens": 12, "cache_read_input_tokens": 100}}}}))
"""
LIMIT = {"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.82, "resetsAt": 1900000000,
    "unifiedWindows": {"seven_day": {"utilization": 0.4, "resetsAt": 1900500000}}}}


def fake_claude(tmp_path, extra=""):
    exe = tmp_path / "claude"
    exe.write_text(FAKE.format(python=sys.executable, extra=extra))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def test_claude_runs_with_only_web_tools_and_a_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "no-debe-llegar")
    home = tmp_path / "home"
    claude = ClaudeCode("tok-123", home, exe=fake_claude(tmp_path), memories=lambda: ["Se llama Ori"])
    events = []
    reply, used = claude.ask("¿qué tiempo hace?", "claude-sonnet-5", "s1", events.append)
    assert reply == "Hace sol en Madrid." and used == ["web_search"]
    assert [e["type"] for e in events] == ["tool", "tool_result"]
    call = json.loads((home / "call.json").read_text())
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert "Bash" in argv[argv.index("--disallowedTools") + 1] and "--strict-mcp-config" in argv
    assert "¿qué tiempo hace?" not in " ".join(argv)  # lo del usuario va por stdin
    assert call["token"] == "tok-123" and "GROQ_API_KEY" not in call["env"] and "API_TOKEN" not in call["env"]
    assert "Se llama Ori" in call["prompt"]
    # Segunda pregunta: continua la misma conversacion.
    claude.ask("¿y mañana?", "claude-sonnet-5", "s1", events.append)
    call = json.loads((home / "call.json").read_text())
    assert call["argv"][-2:] == ["--resume", "abc12345-session"] and call["prompt"] == "¿y mañana?"


def test_claude_endpoint_only_when_configured(tmp_path):
    assistant = make_assistant()
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert "claude_models" not in client.get("/hud/config").json()
    assert client.post("/claude/chat/stream", json={"text": "hola", "model": "claude-sonnet"}, headers=auth).status_code == 404

    assistant.claude = ClaudeCode("tok", tmp_path / "home", exe=fake_claude(tmp_path))
    models = client.get("/hud/config").json()["claude_models"]
    assert [m["id"] for m in models] == ["claude-sonnet-5", "claude-opus-5-5", "claude-fable-5-1", "claude-haiku-4-5"]
    assert [m["label"] for m in models][:2] == ["Claude Sonnet 5", "Claude Opus 5.5"]
    assert client.post("/claude/chat/stream", json={"text": "hola", "model": "claude-sonnet"}).status_code == 401
    assert client.post("/claude/chat/stream", json={"text": "hola", "model": "gpt"}, headers=auth).status_code == 400
    resp = client.post("/claude/chat/stream", json={"text": "hola", "model": "claude-haiku-4-5"}, headers=auth)
    lines = [json.loads(line) for line in resp.text.splitlines()]
    assert lines[0]["type"] == "heard" and lines[-1]["type"] == "done"
    assert lines[-1]["reply"] == "Hace sol en Madrid." and lines[-1]["provider"] == "Claude Haiku 4.5"
    assert lines[-1]["audio_wav_b64"]  # la voz la pone JARVIS
    client.post("/api/reset", json={"session": "default"}, headers=auth)
    assert "default" not in assistant.claude.sessions


def test_claude_versions_usage_and_only_claude(tmp_path):
    from jarvis.claude_code import label

    assert label("claude-haiku-4-5-20251001") == "Claude Haiku 4.5" and label("claude-sonnet-5") == "Claude Sonnet 5"
    claude = ClaudeCode("tok", tmp_path / "home", exe=fake_claude(tmp_path),
                        models=["claude-opus-5-5", "rm -rf /", "claude-opus-5-5"])
    assert claude.model_ids == ["claude-opus-5-5"]  # nada raro llega a la linea de comandos
    fake_claude(tmp_path, extra=f"print({json.dumps(json.dumps(LIMIT))})")
    claude.ask("hola", "claude-opus-5-5", "s", lambda e: None)
    report = claude.usage()
    st = report["models"][0]
    assert st["id"] == "claude-opus-5-5" and st["turns"] == 1 and st["output_tokens"] == 12 and st["cache_tokens"] == 100
    assert report["limits"]["type"] == "five_hour" and report["limits"]["used"] == 0.82
    assert report["limits"]["windows"]["seven_day"]["used"] == 0.4

    assistant = make_assistant()
    assistant.claude, assistant.only_claude = claude, True
    client = TestClient(create_app(assistant, api_token="s"))
    assert client.get("/hud/config").json()["only_claude"] is True
    usage = client.get("/api/usage", headers={"Authorization": "Bearer s"}).json()
    assert usage["claude"]["limits"]["status"] == "allowed_warning"


def test_web_agents_can_work_with_claude_and_fall_back_to_their_chain(tmp_path):
    import pytest

    from jarvis.activity import ActivityLog
    from jarvis.agents import AgentTeam
    from jarvis.tools.registry import Tool, ToolError
    from tests.test_agents import fake_search
    from tests.test_tools import ScriptedLLM

    home = tmp_path / "home"
    claude = ClaudeCode("tok", home, exe=fake_claude(tmp_path))
    activity = ActivityLog()
    script = ScriptedLLM(["RESUMEN: con la cadena.\n# x"])
    team = AgentTeam(script.llm(), {"web_search": fake_search(), "truenas_status": Tool(
        "truenas_status", "estado", {"type": "object", "properties": {}}, lambda _ctx: "ok")}, activity=activity)
    team.claude = claude
    assert "claude-opus-5-5" in team.model_choices("compras")
    assert not [m for m in team.model_choices("tecnico") if m.startswith("claude-")]  # datos privados: nunca Claude
    with pytest.raises(ToolError):
        team.set_model("tecnico", "claude-opus-5-5")

    team.set_model("compras", "claude-opus-5-5")
    job = team.start("compras", "portátil para estudiar", background=False)
    assert job.state == "terminado" and job.model == "Claude Opus 5.5" and job.summary.startswith("Hace sol")
    assert job.steps == ["web_search"] and script.requests == []  # no ha gastado la cadena
    call = json.loads((home / "call.json").read_text())
    assert "Encargo: portátil para estudiar" in call["prompt"] and "WebSearch" in call["prompt"]
    assert "--resume" not in call["argv"] and claude.sessions == {}  # encargo suelto: sin conversacion
    assert claude.usage()["models"][0]["turns"] == 1

    claude.token = ""  # sin membresia (p. ej. se quito el token): sigue con la cadena del agente
    job = team.start("compras", "monitor", background=False)
    assert job.state == "terminado" and job.summary == "con la cadena." and len(script.requests) == 1


def test_agent_task_that_runs_out_of_turns_still_writes_the_report(tmp_path):
    exe = tmp_path / "claude"
    exe.write_text(f"""#!{sys.executable}
import json, sys
prompt = sys.stdin.read()
print(json.dumps({{"type": "system", "session_id": "task-session-1"}}))
if "--resume" in sys.argv:
    assert sys.argv[sys.argv.index("--resume") + 1] == "task-session-1" and "NO uses mas herramientas" in prompt
    print(json.dumps({{"type": "result", "result": "RESUMEN: tres talleres encajan."}}))
else:
    print(json.dumps({{"type": "result", "subtype": "error_max_turns", "is_error": True}}))
""")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    claude = ClaudeCode("tok", tmp_path / "home", exe=str(exe))
    assert claude.task("busca clientes", "claude-sonnet-5", lambda e: None) == "RESUMEN: tres talleres encajan."
    assert claude.usage()["models"][0]["turns"] == 2 and claude.usage()["models"][0]["errors"] == 1


def test_nas_auditor_reviews_code_offline_and_the_server_with_search_only(tmp_path):
    import os

    import pytest

    from jarvis.activity import ActivityLog
    from jarvis.auditor import Auditor, audit_tool, parse_projects
    from jarvis.obsidian import Vault
    from jarvis.tools import ToolContext
    from jarvis.tools.registry import ToolError

    home = tmp_path / "home"
    claude = ClaudeCode("tok", home, exe=fake_claude(tmp_path))
    project = tmp_path / "caliperworks"
    project.mkdir()
    projects = parse_projects(f"caliperworks={project},falta=/no/existe")
    assert set(projects) == {"jarvis", "caliperworks", "falta"}
    activity = ActivityLog()
    (tmp_path / "vault").mkdir()
    auditor = Auditor(claude, lambda: "## SSH\npuerto 22", projects, Vault(tmp_path / "vault"), activity=activity)
    assert auditor.available_projects() == ["jarvis", "caliperworks"]
    with pytest.raises(ToolError, match="se pueden auditar"):
        auditor.start("codigo", "falta")

    auditor.start("codigo", "CaliperWorks", background=False)
    call = json.loads((home / "call.json").read_text())
    argv = call["argv"]
    assert argv[argv.index("--allowedTools") + 1] == "Read,Glob,Grep"  # sin internet
    assert "WebFetch" in argv[argv.index("--disallowedTools") + 1]
    assert 'proyecto "caliperworks"' in call["prompt"]
    assert os.path.samefile(call["cwd"], project)
    kinds = [e["type"] for e in activity.since(0)]
    assert kinds[0] == "agent_start" and kinds[-1] == "agent_done"
    assert list((tmp_path / "vault" / "JARVIS" / "Seguridad").glob("*.md"))

    auditor.start("servidor", background=False)
    call = json.loads((home / "call.json").read_text())
    assert call["argv"][call["argv"].index("--allowedTools") + 1] == "WebSearch"  # buscar, sin abrir paginas
    assert "puerto 22" in call["prompt"]
    tool = audit_tool(auditor)
    assert tool.confirm and not tool.pc and "caliperworks" in tool.description
    assert "Avisará" in tool.fn(ToolContext(), scope="codigo", project="jarvis")


def test_claude_chat_can_start_only_web_agents_through_jarvis(tmp_path, monkeypatch):
    from jarvis import main as jmain
    from jarvis import mcp_agents
    from jarvis.agents import AgentTeam
    from tests.test_agents import fake_search
    from tests.test_tools import ScriptedLLM

    assistant = make_assistant()
    assistant.team = AgentTeam(ScriptedLLM(["RESUMEN: listo.\n# x"]).llm(), {"web_search": fake_search()})
    assistant.claude = ClaudeCode("tok", tmp_path / "home", exe=fake_claude(tmp_path))
    app = create_app(assistant, api_token="s")
    config = json.loads((tmp_path / "home" / "mcp.json").read_text())["mcpServers"]["jarvis"]
    assert config["args"] == ["-m", "jarvis.mcp_agents"] and config["env"]["JARVIS_INTERNAL_TOKEN"]
    assert oct((tmp_path / "home" / "mcp.json").stat().st_mode)[-3:] == "600"

    # En el chat, Claude recibe la config y puede usar agent_run; en los encargos de agentes, no.
    assistant.claude.ask("hola", "claude-sonnet-5", "s", lambda e: None)
    argv = json.loads((tmp_path / "home" / "call.json").read_text())["argv"]
    assert "--mcp-config" in argv and "mcp__jarvis__agent_run" in argv[argv.index("--allowedTools") + 1]
    assistant.claude.task("x", "claude-sonnet-5", lambda e: None)
    assert "--mcp-config" not in json.loads((tmp_path / "home" / "call.json").read_text())["argv"]

    client = TestClient(app)
    internal = {"X-Jarvis-Internal": config["env"]["JARVIS_INTERNAL_TOKEN"]}
    assert client.get("/internal/agents", headers=internal).status_code == 403  # no viene de localhost
    monkeypatch.setattr(jmain, "INTERNAL_HOSTS", {"testclient"})
    assert client.get("/internal/agents", headers={"X-Jarvis-Internal": "otro"}).status_code == 403
    agents = [a["id"] for a in client.get("/internal/agents", headers=internal).json()["agents"]]
    assert agents == ["investigador", "compras", "captador"]  # nunca organizador ni escritor (datos privados)
    assert client.post("/internal/agents/run", json={"agent": "organizador", "task": "mi agenda"},
                       headers=internal).status_code == 400
    out = client.post("/internal/agents/run", json={"agent": "compras", "task": "monitor"}, headers=internal).json()
    assert "se ha puesto con ello" in out["message"]

    # El servidor MCP: protocolo y llamada a JARVIS (aqui con la API simulada).
    calls = []

    def fake_call(method, path, body=None):
        calls.append((method, path, body))
        return {"agents": [{"id": "compras", "description": "compara"}]} if path == "/internal/agents" else {
            "message": "El asesor de compras se ha puesto con ello."}

    monkeypatch.setattr(mcp_agents, "_call", fake_call)
    init = mcp_agents.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "x"}})
    assert init["result"]["protocolVersion"] == "x" and "tools" in init["result"]["capabilities"]
    assert mcp_agents.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp_agents.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert tools[0]["inputSchema"]["properties"]["agent"]["enum"] == ["compras"]
    res = mcp_agents.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "agent_run", "arguments": {"agent": "compras", "task": "monitor"}}})
    assert res["result"]["content"][0]["text"].startswith("El asesor") and not res["result"]["isError"]
    assert calls[-1] == ("POST", "/internal/agents/run", {"agent": "compras", "task": "monitor", "refresh": False})
    assert mcp_agents.handle({"jsonrpc": "2.0", "id": 4, "method": "nada"})["error"]["code"] == -32601
