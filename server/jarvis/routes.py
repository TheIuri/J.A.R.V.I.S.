"""Atajos aprendidos: lo que preguntas una y otra vez deja de pasar por el modelo.

Si la misma pregunta aparece varias veces y siempre acaba llamando a la misma herramienta, no tiene
sentido seguir gastando tokens en decidirlo. Aqui se detectan esos casos y se guardan como "rutas".

Lo importante: una ruta guarda **la herramienta, no la respuesta**. Cada vez que la usas se vuelve a
llamar a la herramienta, asi que los datos salen del momento; si cambian, cambia la respuesta. Nunca
se sirve nada cacheado.

Dos frenos, para que esto no se estropee solo:
- Solo se proponen rutas cuyo resultado ya es una frase clara en espanol (ver `speakable`). Si la
  herramienta devuelve una tabla, JSON o un error, no vale como respuesta directa.
- Ninguna ruta se activa sola: salen como propuesta en el repaso semanal y las apruebas tu.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .shortcuts import match as builtin_match
from .shortcuts import plain
from .tools import ToolContext, ToolRegistry

log = logging.getLogger("jarvis.routes")

MIN_TIMES = 4  # veces que hay que repetir una pregunta para que merezca un atajo
MAX_ROUTES = 40
MAX_REPLY = 300  # una respuesta directa es una frase, no un informe
MIN_WORDS = 2
# Herramientas que nunca se aprenden solas: cambian cosas, cuestan dinero o piden confirmacion.
NEVER = {"agent_run", "agent_plan", "agent_create", "agent_delete", "delegate_claude", "security_audit",
         "calendar_add", "reminder_set", "reminder_cancel", "memory_save", "memory_update", "memory_forget",
         "obsidian_create_note", "obsidian_append", "obsidian_daily_note", "truenas_app_restart",
         "home_control", "wake_on_lan", "pc_open_app", "pc_open_url", "pc_timer", "price_watch",
         "spotify_play", "lead_update"}


def speakable(text: str) -> bool:
    """Si el resultado de la herramienta se puede decir tal cual: una o dos frases limpias."""
    clean = (text or "").strip()
    if not clean or len(clean) > MAX_REPLY or clean.startswith("ERROR"):
        return False
    if "\n" in clean or "|" in clean or clean.startswith(("{", "[")):
        return False
    return len(clean.split()) >= 3


def shape(text: str) -> str:
    """La forma de la pregunta, sin cortesias ni numeros, para ver si es 'la misma de siempre'."""
    p = plain(text).removeprefix("jarvis ").removesuffix(" jarvis").removesuffix(" por favor")
    p = re.sub(r"\b\d+\b", "#", p)
    return " ".join(p.split())


@dataclass
class Route:
    shape: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    times: int = 0  # veces que se vio antes de aprenderla
    created: str = ""

    def line(self) -> str:
        extra = f" {self.args}" if self.args else ""
        return f"«{self.shape}» → {self.tool}{extra}"


class Routes:
    """Las rutas aprobadas, en disco. Se consultan antes de molestar al modelo."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.items: dict[str, Route] = {}
        if self.path and self.path.exists():
            try:
                for raw in json.loads(self.path.read_text(encoding="utf-8")):
                    route = Route(raw["shape"], raw["tool"], raw.get("args") or {}, raw.get("times", 0),
                                  raw.get("created", ""))
                    self.items[route.shape] = route
            except (OSError, ValueError, KeyError, TypeError):
                log.warning("no se han podido leer los atajos aprendidos")

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([vars(r) for r in self.items.values()], ensure_ascii=False, indent=1),
                             encoding="utf-8")

    def add(self, route: Route) -> Route:
        with self._lock:
            if len(self.items) >= MAX_ROUTES and route.shape not in self.items:
                raise ValueError(f"ya hay {MAX_ROUTES} atajos aprendidos")
            self.items[route.shape] = route
            self._save()
        return route

    def remove(self, shape_: str) -> bool:
        with self._lock:
            gone = self.items.pop(shape_, None) is not None
            if gone:
                self._save()
        return gone

    def find(self, text: str) -> Route | None:
        return self.items.get(shape(text))

    def answer(self, text: str, tools: ToolRegistry | None, ctx: ToolContext) -> tuple[str, str] | None:
        """(respuesta, herramienta) llamando a la herramienta ahora mismo, o None si no hay ruta o falla.

        Los datos son siempre los de este momento: la ruta guarda a quien preguntar, no la respuesta."""
        route = self.find(text) if tools else None
        if route is None or route.tool not in tools.names():
            return None
        result = tools.execute(route.tool, json.dumps(route.args), ctx)
        if not speakable(result):  # la herramienta ya no contesta como cuando se aprendio: mejor el modelo
            log.info("atajo %r descartado en este turno: la herramienta no da una frase", route.shape)
            return None
        return result.strip(), route.tool


def candidates(turns: list[dict], known: dict[str, Route] | None = None) -> list[Route]:
    """Preguntas que se repiten y siempre acaban en la misma herramienta: candidatas a atajo.

    Se descartan las que ya contesta un atajo de serie, las que llevan pulgar abajo alguna vez y las
    herramientas de la lista NEVER (las que cambian algo)."""
    by_shape: dict[str, list[dict]] = {}
    for turn in turns:
        text = (turn.get("user") or "").strip()
        key = shape(text)
        if len(key.split()) < MIN_WORDS or builtin_match(text):
            continue  # vacia, o ya la contesta un atajo de los de serie
        by_shape.setdefault(key, []).append(turn)
    out = []
    for key, group in by_shape.items():
        if known and key in known:
            continue
        if len(group) < MIN_TIMES or any(t.get("verdict") == "mal" for t in group):
            continue
        used = Counter(tuple((t.get("tools") or "").split(",")) for t in group)
        (tools_used, veces), = used.most_common(1)
        if veces < MIN_TIMES or len(tools_used) != 1 or not tools_used[0]:
            continue  # no siempre la misma herramienta, o ninguna: no hay ruta clara
        tool = tools_used[0]
        if tool in NEVER:
            continue
        out.append(Route(key, tool, {}, len(group)))
    return sorted(out, key=lambda r: -r.times)
