"""Estado del TrueNAS via su API oficial (JSON-RPC por websocket), en solo lectura.

Usa una API key de un usuario con rol de solo lectura. En TrueNAS 26 la key se valida con
SCRAM (nunca viaja por la red) y va siempre sobre wss://.
"""

from __future__ import annotations

from typing import Any, Callable

from .registry import Tool, ToolContext, ToolError

GiB = 1024**3


def _gib(n: Any) -> str:
    return f"{(n or 0) / GiB:,.0f} GiB".replace(",", ".")


def summarize(section: str, call: Callable[..., Any]) -> str:
    """Convierte las respuestas de la API en texto corto para el LLM."""
    lines: list[str] = []
    if section in ("resumen", "sistema"):
        info = call("system.info")
        uptime_h = int(info.get("uptime_seconds", 0) // 3600)
        load = info.get("loadavg") or []
        lines.append(
            f"Sistema {info.get('hostname', '?')} (TrueNAS {info.get('version', '?')}), "
            f"encendido {uptime_h // 24} d {uptime_h % 24} h"
            + (f", carga {load[0]:.2f}" if load else "")
            + "."
        )
    if section in ("resumen", "discos"):
        for pool in call("pool.query"):
            size, free = pool.get("size"), pool.get("free")
            used = f", {100 * (size - free) / size:.0f}% usado, libres {_gib(free)}" if size and free is not None else ""
            health = "sano" if pool.get("healthy") else "CON PROBLEMAS"
            lines.append(f"Pool {pool.get('name')}: {pool.get('status')} ({health}){used}.")
    if section in ("resumen", "apps"):
        apps = call("app.query")
        running = [a["name"] for a in apps if a.get("state") == "RUNNING"]
        other = [f"{a['name']} ({a.get('state', '?').lower()})" for a in apps if a.get("state") != "RUNNING"]
        updates = [a["name"] for a in apps if a.get("upgrade_available")]
        lines.append(f"Apps en marcha ({len(running)}): {', '.join(running) or 'ninguna'}.")
        if other:
            lines.append(f"Apps paradas o con fallos: {', '.join(other)}.")
        if updates:
            lines.append(f"Con actualización disponible: {', '.join(updates)}.")
    if section in ("resumen", "alertas"):
        alerts = [a for a in call("alert.list") if not a.get("dismissed")]
        if not alerts:
            lines.append("Sin alertas activas.")
        for a in alerts[:5]:
            lines.append(f"Alerta {a.get('level', '?')}: {a.get('formatted') or a.get('text', '')}".strip())
        if len(alerts) > 5:
            lines.append(f"... y {len(alerts) - 5} alertas más.")
    return "\n".join(lines)


def _default_connect(url: str, username: str, api_key: str, verify_ssl: bool):
    from truenas_api_client import Client  # import perezoso: solo hace falta si se configura

    client = Client(url, verify_ssl=verify_ssl, call_timeout=10)
    try:
        client.login_with_api_key(username, api_key)
    except Exception:
        client.close()
        raise
    return client


def truenas_tool(url: str, username: str, api_key: str, verify_ssl: bool, connect=_default_connect) -> Tool:
    def run(_ctx: ToolContext, section: str = "resumen") -> str:
        try:
            client = connect(url, username, api_key, verify_ssl)
        except Exception as exc:
            raise ToolError(f"no puedo conectar con TrueNAS: {exc}") from exc
        try:
            return summarize(section, client.call)
        except Exception as exc:
            raise ToolError(f"error consultando TrueNAS: {exc}") from exc
        finally:
            client.close()

    return Tool(
        name="truenas_status",
        description="Estado del servidor TrueNAS del usuario: sistema, discos/pools, apps y alertas.",
        parameters={
            "type": "object",
            "properties": {
                "section": {"type": "string", "enum": ["resumen", "sistema", "discos", "apps", "alertas"]},
            },
        },
        fn=run,
        timeout_s=20,
    )
