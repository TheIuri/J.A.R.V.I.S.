"""Vigilantes (Nivel 4): comprueban cosas cada cierto tiempo y publican avisos.

Cada comprobacion devuelve avisos (clave, nivel, origen, texto). La clave evita repetir el
mismo aviso; incluye el dato que cambia (fecha, estado...) para que un problema nuevo si avise.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .notify import NoticeBoard
from .tools.calendar import Calendars
from .tools.reminders import ReminderStore
from .tools.truenas import OK_STATES, _task_state

log = logging.getLogger("jarvis.watch")

Alert = tuple[str, str, str, str]  # (clave, nivel, origen, texto)


@dataclass
class Check:
    name: str
    interval_s: float
    fn: Callable[[], list[Alert]]
    next_run: float = 0.0


class Watcher:
    def __init__(self, board: NoticeBoard, checks: list[Check]):
        self.board = board
        self.checks = checks
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="watcher", daemon=True)

    def start(self) -> None:
        if self.checks:
            log.info("Vigilantes: %s", ", ".join(f"{c.name} (cada {c.interval_s:.0f}s)" for c in self.checks))
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for check in self.checks:
            if now < check.next_run:
                continue
            check.next_run = now + check.interval_s
            try:
                alerts = check.fn()
            except Exception as exc:  # un servicio caido no debe parar al resto
                log.warning("vigilante %s: %s", check.name, exc)
                continue
            for key, level, source, text in alerts:
                self.board.post(level, source, text, key)

    def _run(self) -> None:
        while not self._stop.wait(5):
            self.run_once()


# --- comprobaciones ---------------------------------------------------------------------


def reminders_check(store: ReminderStore) -> Check:
    def fn() -> list[Alert]:
        return [(f"reminder:{r.id}", "info", "recordatorio", f"Recordatorio: {r.text}.") for r in store.take_due()]

    return Check("recordatorios", 20, fn)


def calendar_check(calendars: Calendars, minutes: int = 15) -> Check:
    """Avisa de cada cita (no las de dia completo) unos minutos antes."""

    def fn() -> list[Alert]:
        now = datetime.now(calendars.tz)
        alerts = []
        for ev in calendars.events(now.date(), 2):
            if ev.all_day or not now < ev.start <= now + timedelta(minutes=minutes):
                continue
            left = max(1, round((ev.start - now).total_seconds() / 60))
            where = f" en {ev.location}" if ev.location else ""
            alerts.append(
                (f"cal:{ev.calendar}:{ev.title}:{ev.start.isoformat()}", "info", "agenda",
                 f"En {left} minutos: {ev.title}{where}.")
            )
        return alerts

    return Check("agenda", 60, fn)


def truenas_check(
    connect: Callable[[], Any], temp_warn: float = 50, interval_s: float = 300
) -> Check:
    """Pools con problemas, discos calientes, apps caidas (y recuperadas), alertas nuevas y copias fallidas."""
    previous_apps: dict[str, str] = {}

    def fn() -> list[Alert]:
        client = connect()
        try:
            call = client.call
            alerts: list[Alert] = []
            today = datetime.now().strftime("%Y-%m-%d")
            for pool in call("pool.query"):
                if not pool.get("healthy", True) or pool.get("status") not in ("ONLINE", None):
                    alerts.append((f"pool:{pool.get('name')}:{pool.get('status')}", "critical", "TrueNAS",
                                   f"El pool {pool.get('name')} tiene problemas: {pool.get('status')}."))
            for disk, temp in (call("disk.temperatures") or {}).items():
                temp = temp.get("temp") if isinstance(temp, dict) else temp
                if isinstance(temp, (int, float)) and temp >= temp_warn:
                    alerts.append((f"temp:{disk}:{today}", "warning", "TrueNAS",
                                   f"El disco {disk} está a {temp:.0f} grados."))
            states = {a["name"]: a.get("state", "?") for a in call("app.query")}
            for name, state in states.items():
                before = previous_apps.get(name)
                if before == "RUNNING" and state in ("CRASHED", "STOPPED"):
                    alerts.append((f"app:{name}:{state}:{time.time():.0f}", "warning", "TrueNAS",
                                   f"La app {name} se ha {'caído' if state == 'CRASHED' else 'parado'}."))
                elif before in ("CRASHED", "STOPPED") and state == "RUNNING":
                    alerts.append((f"app:{name}:up:{time.time():.0f}", "info", "TrueNAS",
                                   f"La app {name} vuelve a funcionar."))
            previous_apps.clear()
            previous_apps.update(states)
            for a in call("alert.list"):
                level = str(a.get("level", "")).upper()
                if a.get("dismissed") or level not in ("WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY"):
                    continue
                text = (a.get("formatted") or a.get("text") or "").strip().split("\n")[0][:200]
                alerts.append((f"alert:{a.get('uuid') or a.get('id') or text}",
                               "critical" if level in ("CRITICAL", "ALERT", "EMERGENCY") else "warning",
                               "TrueNAS", f"Alerta de TrueNAS: {text}"))
            for label, method, name in (
                ("La replicación", "replication.query", lambda t: t.get("name", "?")),
                ("La copia en la nube", "cloudsync.query", lambda t: t.get("description") or t.get("path", "?")),
                ("El snapshot de", "pool.snapshottask.query", lambda t: t.get("dataset", "?")),
            ):
                for task in call(method):
                    state, when, _error = _task_state(task)
                    if task.get("enabled", True) and state in ("ERROR", "FAILED"):
                        alerts.append((f"backup:{method}:{task.get('id')}:{when}", "warning", "TrueNAS",
                                       f"{label} {name(task)} ha fallado."))
            return alerts
        finally:
            client.close()

    return Check("truenas", interval_s, fn)


__all__ = ["Check", "Watcher", "calendar_check", "reminders_check", "truenas_check", "OK_STATES"]
