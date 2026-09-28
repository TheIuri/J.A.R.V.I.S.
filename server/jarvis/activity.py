"""Trazabilidad en directo de los agentes: que hacen, con que modelo y que herramientas usan.

El HUD lo recoge con espera larga (/api/activity) y lo dibuja en el cerebro 3D y en la linea de
tiempo. Solo en memoria (los ultimos eventos); lo duradero son los informes en Obsidian.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Any

MAX_EVENTS = 200


class ActivityLog:
    def __init__(self):
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._next = 1
        self._cond = threading.Condition()

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        with self._cond:
            event = {"id": self._next, "ts": datetime.now().isoformat(timespec="seconds"), "type": kind, **data}
            self._next += 1
            self._events.append(event)
            self._cond.notify_all()
        return event

    @property
    def last_id(self) -> int:
        return self._next - 1

    def since(self, after: int, wait_s: float = 0) -> list[dict[str, Any]]:
        with self._cond:
            if wait_s and self.last_id <= after:
                self._cond.wait(wait_s)
            return [e for e in self._events if e["id"] > after]
