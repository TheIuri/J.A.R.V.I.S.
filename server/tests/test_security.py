from fastapi.testclient import TestClient

from jarvis.activity import ActivityLog
from jarvis.main import create_app
from jarvis.tools import ToolContext
from jarvis.tools.security import audit_tool, security_snapshot
from tests.test_core import make_assistant
from tests.test_tools import registry

API = {
    "system.info": {"version": "26.0.0", "hostname": "truenas", "uptime_seconds": 864000},
    "system.general.config": {"ui_port": 80, "ui_httpsport": 443, "ui_httpsredirect": False, "ui_address": ["0.0.0.0"]},
    "service.query": [{"service": "ssh", "state": "RUNNING", "enable": True}, {"service": "ftp", "state": "STOPPED", "enable": False}],
    "ssh.config": {"tcpport": 22, "passwordauth": True, "kerberosauth": False, "tcpfwd": True, "bindiface": []},
    "smb.config": {"enable_smb1": False, "guest": "nobody", "ntlmv1_auth": False},
    "sharing.smb.query": [{"name": "obsidian", "path": "/mnt/Data/obsidian", "enabled": True, "guestok": False}],
    "user.query": [{"username": "ori", "uid": 3000, "sudo_commands_nopasswd": ["ALL"], "sshpubkey": "ssh-ed25519 AAAA SECRETO",
                    "ssh_password_enabled": True, "shell": "/usr/bin/zsh", "unixhash": "$6$HASH"}],
    "certificate.query": [{"name": "truenas_default", "until": {"$date": 1790000000000}, "issuer": "self"}],
    "app.query": [{"name": "jarvis-ai", "state": "RUNNING", "version": "1.0", "upgrade_available": True,
                   "active_workloads": {"used_ports": [{"container_port": 8765, "host_ports": [{"host_ip": "0.0.0.0", "host_port": 8765}]}]}}],
    "alert.list": [{"level": "WARNING", "formatted": "Certificado a punto de caducar\nmás detalle", "dismissed": False}],
}


def call(method, *args):
    if method == "auth.twofactor.config":
        raise PermissionError("sin permiso")
    return API[method]


def test_snapshot_covers_the_risky_bits_without_secrets():
    text = security_snapshot(call)
    for expected in [
        "TrueNAS 26.0.0", "redirigir a HTTPS: False", "ssh: EN MARCHA, arranca al iniciar", "login con contraseña: True",
        "SMB1 activado: False", "sudo SIN contraseña", "clave SSH si", "caduca 2026-", "ACTUALIZACION DISPONIBLE",
        "puertos 0.0.0.0:8765->8765", "WARNING: Certificado a punto de caducar",
        "## Doble factor\n(no disponible: PermissionError)",
    ]:
        assert expected in text, expected
    assert "SECRETO" not in text and "HASH" not in text and "más detalle" not in text


def test_audit_tool_needs_a_yes_and_ships_the_snapshot_to_the_pc():
    tool = audit_tool(lambda: "FOTO DEL SERVIDOR")
    reg = registry(tool)
    assert reg.specs(ToolContext()) == []  # solo con el HUD del PC
    spec = reg.specs(ToolContext(pc_apps=[]))[0]["function"]
    assert spec["parameters"]["properties"]["scope"]["enum"] == ["servidor", "codigo"]
    ctx = ToolContext(pc_apps=[])
    assert "PENDIENTE DE CONFIRMACION" in reg.execute("security_audit", '{"scope": "servidor"}', ctx)
    assert ctx.pending and ctx.pc_actions == []  # nada hasta que digas que sí
    ok, _ = reg.run_confirmed(ctx.pending, ctx)
    assert ok and ctx.pc_actions == [{"action": "audit", "scope": "servidor", "context": "FOTO DEL SERVIDOR"}]
    code = ToolContext(pc_apps=[])
    reg.run_confirmed(type(ctx.pending)(tool, {"scope": "codigo", "project": "CaliperWorks"}, "x"), code)
    assert code.pc_actions == [{"action": "audit", "scope": "codigo", "project": "caliperworks"}]
    assert audit_tool(None).parameters["properties"]["scope"]["enum"] == ["codigo"]  # sin TrueNAS


def test_external_agent_events_and_reports(tmp_path):
    from jarvis.notify import NoticeBoard
    from jarvis.obsidian import Vault

    assistant = make_assistant()
    assistant.activity = ActivityLog()
    assistant.vault = Vault(tmp_path)
    assistant.board = NoticeBoard()
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert client.post("/api/agent_event", json={"type": "agent_start", "agent": "auditor"}).status_code == 401
    assert client.post("/api/agent_event", json={"type": "agent_start", "agent": "hacker"}, headers=auth).status_code == 400
    assert client.post("/api/agent_event", json={"type": "rm -rf", "agent": "auditor"}, headers=auth).status_code == 400
    client.post("/api/agent_event", json={"type": "agent_start", "agent": "auditor", "task": "seguridad del servidor"}, headers=auth)
    client.post("/api/agent_event", json={"type": "agent_tool", "agent": "auditor", "tool": "web_search"}, headers=auth)
    report = "RESUMEN: Dos riesgos altos.\n# Auditoría\n" + "hallazgo detallado. " * 400  # más de 4000 caracteres
    out = client.post("/api/agent_result", json={"title": "seguridad del servidor", "text": report, "source": "auditor"},
                      headers=auth).json()
    assert out["note"].startswith("JARVIS/Seguridad/") and len((tmp_path / out["note"]).read_text()) > 7000
    events = assistant.activity.since(0)
    assert [(e["type"], e["agent"]) for e in events] == [
        ("agent_start", "auditor"), ("agent_tool", "auditor"), ("agent_done", "auditor")]
    assert events[0]["label"] == "El auditor de seguridad"
    assert assistant.board.since(0)[0].text.startswith("El auditor de seguridad ha terminado")
