"""Auditor de ciberseguridad (Nivel 5): lo analiza Claude con la membresia del usuario, en su PC.

Dos modos:
- servidor: este codigo recopila una "foto" de seguridad de TrueNAS (servicios, SSH, SMB,
  usuarios con privilegios, 2FA, certificados, apps y puertos, comparticiones, alertas) y se la
  pasa a Claude, que solo puede BUSCAR en internet (vulnerabilidades conocidas): sin abrir
  paginas, porque lleva datos de tu servidor.
- codigo: Claude revisa un proyecto del PC (de una lista permitida en el propio PC) solo
  leyendo archivos y SIN internet: no hay por donde sacar nada.
Siempre pide confirmacion (gasta cupo de la membresia) y solo desde el HUD del PC.
La foto nunca incluye secretos: de las claves SSH solo se dice si existen.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .registry import Tool, ToolContext, ToolError

MAX_SNAPSHOT_CHARS = 12000


def _date(value: Any) -> str:
    if isinstance(value, dict) and "$date" in value:
        value = datetime.fromtimestamp(value["$date"] / 1000, tz=timezone.utc)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value or "?")


def security_snapshot(call: Callable[..., Any]) -> str:
    """Texto compacto con lo relevante para una auditoria. Cada seccion es independiente."""
    sections: list[str] = []

    def section(title: str, fn: Callable[[], list[str]]) -> None:
        try:
            lines = fn()
        except Exception as exc:  # sin permiso o metodo inexistente en esta version: se omite
            lines = [f"(no disponible: {type(exc).__name__})"]
        sections.append(f"## {title}\n" + ("\n".join(lines) if lines else "(nada)"))

    def system() -> list[str]:
        info = call("system.info")
        return [f"TrueNAS {info.get('version', '?')}, host {info.get('hostname', '?')}, uptime {int(info.get('uptime_seconds', 0) // 86400)} dias"]

    def ui() -> list[str]:
        g = call("system.general.config")
        return [
            f"UI HTTP puerto {g.get('ui_port')}, HTTPS puerto {g.get('ui_httpsport')}, "
            f"redirigir a HTTPS: {g.get('ui_httpsredirect')}, escucha en: {g.get('ui_address')}"
        ]

    def services() -> list[str]:
        return [
            f"{s.get('service')}: {'EN MARCHA' if s.get('state') == 'RUNNING' else 'parado'}"
            f"{', arranca al iniciar' if s.get('enable') else ''}"
            for s in call("service.query")
        ]

    def ssh() -> list[str]:
        c = call("ssh.config")
        return [
            f"puerto {c.get('tcpport')}, login con contraseña: {c.get('passwordauth')}, "
            f"kerberos: {c.get('kerberosauth')}, reenvio TCP: {c.get('tcpfwd')}, "
            f"interfaces: {c.get('bindiface') or 'todas'}"
        ]

    def smb() -> list[str]:
        c = call("smb.config")
        lines = [f"SMB1 activado: {c.get('enable_smb1')}, invitado: {c.get('guest')}, NTLMv1: {c.get('ntlmv1_auth')}"]
        for share in call("sharing.smb.query"):
            lines.append(
                f"comparticion {share.get('name')} -> {share.get('path')}: "
                f"{'activa' if share.get('enabled') else 'desactivada'}, invitados: {share.get('guestok', False)}"
            )
        return lines

    def users() -> list[str]:
        lines = []
        for u in call("user.query", [["builtin", "=", False]]):
            sudo = bool(u.get("sudo_commands") or u.get("sudo_commands_nopasswd"))
            nopass = bool(u.get("sudo_commands_nopasswd"))
            lines.append(
                f"{u.get('username')} (uid {u.get('uid')}): sudo {'SIN contraseña' if nopass else sudo}, "
                f"SSH con contraseña {u.get('ssh_password_enabled', False)}, clave SSH {'si' if u.get('sshpubkey') else 'no'}, "
                f"bloqueado {u.get('locked', False)}, contraseña desactivada {u.get('password_disabled', False)}, "
                f"shell {u.get('shell')}"
            )
        return lines

    def twofactor() -> list[str]:
        c = call("auth.twofactor.config")
        return [f"2FA activado: {c.get('enabled')}, tambien para SSH: {c.get('services', {}).get('ssh', False)}"]

    def certificates() -> list[str]:
        return [f"{c.get('name')}: caduca {_date(c.get('until'))}, emisor {c.get('issuer') or '?'}" for c in call("certificate.query")]

    def apps() -> list[str]:
        lines = []
        for a in call("app.query"):
            ports = []
            for p in (a.get("active_workloads") or {}).get("used_ports", []) or []:
                for hp in p.get("host_ports", []) or []:
                    ports.append(f"{hp.get('host_ip', '0.0.0.0')}:{hp.get('host_port')}->{p.get('container_port')}")
            lines.append(
                f"{a.get('name')} ({a.get('state')}), version {a.get('version', '?')}"
                f"{', ACTUALIZACION DISPONIBLE' if a.get('upgrade_available') else ''}"
                f"{', puertos ' + ', '.join(ports) if ports else ''}"
            )
        return lines

    def alerts() -> list[str]:
        return [
            f"{a.get('level')}: {(a.get('formatted') or a.get('text') or '').splitlines()[0][:200]}"
            for a in call("alert.list")
            if not a.get("dismissed")
        ]

    section("Sistema", system)
    section("Interfaz web", ui)
    section("Servicios", services)
    section("SSH", ssh)
    section("SMB", smb)
    section("Usuarios (no del sistema)", users)
    section("Doble factor", twofactor)
    section("Certificados", certificates)
    section("Apps y puertos publicados", apps)
    section("Alertas activas", alerts)
    text = "\n\n".join(sections)
    return text if len(text) <= MAX_SNAPSHOT_CHARS else text[:MAX_SNAPSHOT_CHARS] + "\n[... recortado]"


def audit_tool(snapshot: Callable[[], str] | None) -> Tool:
    """snapshot: funcion que devuelve la foto del servidor (None si TrueNAS no esta configurado)."""
    scopes = (["servidor"] if snapshot else []) + ["codigo"]

    def run(ctx: ToolContext, scope: str, project: str = "") -> str:
        if scope == "servidor":
            if snapshot is None:
                raise ToolError("TrueNAS no está configurado")
            ctx.pc_actions.append({"action": "audit", "scope": "servidor", "context": snapshot()})
            return "Auditoría del servidor encargada a Claude en tu PC; te aviso al terminar."
        if not project.strip():
            raise ToolError("¿qué proyecto audito? (p. ej. jarvis o caliperworks)")
        ctx.pc_actions.append({"action": "audit", "scope": "codigo", "project": project.strip().lower()})
        return f"Auditoría del código de {project} encargada a Claude en tu PC; te aviso al terminar."

    return Tool(
        name="security_audit",
        description=(
            "Auditoría de ciberseguridad hecha por Claude con la membresía del usuario (tarda unos minutos, guarda el "
            "informe en Obsidian y avisa). scope=servidor: revisa la configuración de seguridad del TrueNAS. "
            "scope=codigo: revisa el código de un proyecto del PC (project, p. ej. 'jarvis' o 'caliperworks')."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": scopes},
                "project": {"type": "string", "description": "Solo para scope=codigo"},
            },
            "required": ["scope"],
        },
        fn=run,
        pc=True,
        confirm=True,
        describe=lambda a: (
            "que Claude audite la seguridad del servidor TrueNAS (gasta cupo de tu membresía)"
            if a.get("scope") == "servidor"
            else f"que Claude audite el código de {a.get('project') or '?'} (gasta cupo de tu membresía)"
        ),
        timeout_s=40,
    )
