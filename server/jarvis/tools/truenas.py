"""TrueNAS via su API oficial (JSON-RPC por websocket).

- truenas_status: solo lectura (sistema, pools, temperaturas, apps, copias, alertas).
- truenas_app_restart: reinicia una app, solo si el usuario dice "si" al preguntarle
  (la API key necesita permiso para gestionar apps; con una de solo lectura dara error).
En TrueNAS 26 la key se valida con SCRAM (nunca viaja por la red) y va siempre sobre wss://.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .registry import Tool, ToolContext, ToolError

GiB = 1024**3


def _gib(n: Any) -> str:
    return f"{(n or 0) / GiB:,.0f} GiB".replace(",", ".")


def _when(value: Any) -> str:
    """Fechas de la API: datetime o {"$date": milisegundos}."""
    if isinstance(value, dict) and "$date" in value:
        value = datetime.fromtimestamp(value["$date"] / 1000, tz=timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone().strftime("%d/%m %H:%M")
    return ""


def _task_state(task: dict) -> tuple[str, str, str]:
    """(estado, cuando, error) de una tarea de copia; unas usan "state" y otras "job"."""
    info = task.get("job") or task.get("state") or {}
    if not isinstance(info, dict):
        return str(info), "", ""
    when = _when(info.get("time_finished") or info.get("datetime"))
    return str(info.get("state") or "SIN EJECUTAR"), when, str(info.get("error") or "")


OK_STATES = {"SUCCESS", "FINISHED"}


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
    if section in ("resumen", "temperaturas"):
        temps = call("disk.temperatures") or {}
        values = {d: (t.get("temp") if isinstance(t, dict) else t) for d, t in temps.items()}
        values = {d: t for d, t in values.items() if isinstance(t, (int, float))}
        if values:
            hot = max(values.values())
            detail = ", ".join(f"{d} {t:.0f} °C" for d, t in sorted(values.items()))
            if section == "temperaturas":
                lines.append(f"Temperatura de los discos: {detail}.")
            else:
                lines.append(f"Discos entre {min(values.values()):.0f} y {hot:.0f} °C.")
            if hot >= 50:
                lines.append(f"Atención: algún disco está a {hot:.0f} °C (más de 50 °C es mucho).")
        elif section == "temperaturas":
            lines.append("TrueNAS no da la temperatura de los discos.")
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
    if section in ("resumen", "copias"):
        tasks = [
            ("Snapshot", "pool.snapshottask.query", lambda t: t.get("dataset", "?")),
            ("Replicación", "replication.query", lambda t: t.get("name", "?")),
            ("Cloud Sync", "cloudsync.query", lambda t: t.get("description") or t.get("path", "?")),
        ]
        failed, total = [], 0
        for label, method, name in tasks:
            for task in call(method):
                if not task.get("enabled", True):
                    continue
                total += 1
                state, when, error = _task_state(task)
                ok = state in OK_STATES or state == "RUNNING" or state == "PENDING"
                if section == "copias":
                    extra = f" — {error[:120]}" if error and not ok else ""
                    lines.append(f"{label} {name(task)}: {state.lower()}{f' ({when})' if when else ''}{extra}.")
                elif not ok:
                    failed.append(f"{label} {name(task)} ({state.lower()})")
        if section == "copias" and not total:
            lines.append("No hay tareas de copia (snapshots, replicación ni Cloud Sync) activas.")
        if section == "resumen" and total:
            lines.append(f"Copias con fallos: {', '.join(failed)}." if failed else f"Copias ({total} tareas): todas bien.")
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


def truenas_tools(url: str, username: str, api_key: str, verify_ssl: bool, connect=_default_connect) -> list[Tool]:
    def with_client(fn: Callable[[Callable[..., Any]], str]) -> str:
        try:
            client = connect(url, username, api_key, verify_ssl)
        except Exception as exc:
            raise ToolError(f"no puedo conectar con TrueNAS: {exc}") from exc
        try:
            return fn(client.call)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"error de TrueNAS: {exc}") from exc
        finally:
            client.close()

    def status(_ctx: ToolContext, section: str = "resumen") -> str:
        return with_client(lambda call: summarize(section, call))

    def restart(_ctx: ToolContext, app: str) -> str:
        def run(call):
            names = [a["name"] for a in call("app.query")]
            match = next((n for n in names if n.lower() == app.strip().lower()), None)
            if match is None:
                raise ToolError(f"no hay ninguna app llamada '{app}'; apps: {', '.join(names)}")
            call("app.redeploy", match, job=True)  # recrea sus contenedores (= reiniciarla)
            return f"La app {match} se ha reiniciado."

        return with_client(run)

    return [
        Tool(
            name="truenas_status",
            description=(
                "Estado del servidor TrueNAS del usuario: sistema, discos/pools, temperaturas de los discos, "
                "apps (y las caídas), copias de seguridad (snapshots, replicación, Cloud Sync) y alertas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["resumen", "sistema", "discos", "temperaturas", "apps", "copias", "alertas"],
                    },
                },
            },
            fn=status,
            timeout_s=20,
        ),
        Tool(
            name="truenas_app_restart",
            description="Reinicia una app de TrueNAS (p. ej. si está caída). Siempre se pide confirmación al usuario.",
            parameters={
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Nombre de la app en TrueNAS"}},
                "required": ["app"],
            },
            fn=restart,
            confirm=True,
            describe=lambda args: f"reiniciar la app {args.get('app')} de TrueNAS",
            timeout_s=120,
        ),
    ]
