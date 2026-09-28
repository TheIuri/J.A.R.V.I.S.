"""Fichas de las respuestas: los datos clave de lo que JARVIS acaba de decir, para el HUD.

Despues de un turno con datos (usó herramientas o la respuesta es larga), un hilo aparte le pide al
modelo que saque hasta cuatro datos concretos (cifras, fechas, nombres) y la ficha aparece en el HUD
por el feed de actividad. No retrasa la respuesta: si el modelo tarda o falla, simplemente no hay
ficha. El modelo solo ve la pregunta y la respuesta que ya vio en el turno; no hay datos nuevos.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from datetime import datetime
from typing import Any

from .activity import ActivityLog
from .llm import FallbackLLM, LLMError

log = logging.getLogger("jarvis.insights")

MIN_REPLY_CHARS = 160  # sin herramientas, solo respuestas con algo de chicha
MAX_ITEMS = 4
KEEP = 12

PROMPT = """Saca los datos clave de esta respuesta de un asistente para una ficha resumen.
Devuelve SOLO un JSON, sin texto alrededor:
{"titulo": "<de 2 a 4 palabras>", "datos": [{"k": "<etiqueta corta>", "v": "<valor corto: cifra, fecha, nombre, lugar>"}]}
Maximo 4 datos, solo los que aparezcan en la respuesta (no inventes). Valores de menos de 40 caracteres.
Si no hay datos concretos que merezcan ficha, devuelve {"datos": []}. En espanol."""


def parse_card(text: str) -> dict[str, Any] | None:
    """El JSON del modelo -> {"title", "items": [{"k", "v"}]}; None si no hay nada util."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    items = []
    for item in data.get("datos") or []:
        if not isinstance(item, dict):
            continue
        k, v = str(item.get("k") or "").strip(), str(item.get("v") or "").strip()
        if k and v:
            items.append({"k": k[:40], "v": v[:60]})
    if not items:
        return None
    title = str(data.get("titulo") or "").strip()[:50] or "Datos clave"
    return {"title": title, "items": items[:MAX_ITEMS]}


class Insights:
    def __init__(self, llm: FallbackLLM, activity: ActivityLog | None):
        self.llm = llm
        self.activity = activity
        self.recent: deque[dict[str, Any]] = deque(maxlen=KEEP)
        self._busy = threading.Lock()

    @staticmethod
    def worth(reply: str, tools_used: list[str]) -> bool:
        return bool(reply.strip()) and (bool(tools_used) or len(reply) >= MIN_REPLY_CHARS)

    def submit(self, question: str, reply: str, tools_used: list[str], model: str | None = None) -> bool:
        """Lanza la ficha en segundo plano. Si ya hay una en marcha, esta se salta (nunca se acumulan)."""
        if not self.worth(reply, tools_used) or not self._busy.acquire(blocking=False):
            return False
        threading.Thread(
            target=self._run, args=(question, reply, list(tools_used), model), name="insight", daemon=True
        ).start()
        return True

    def _run(self, question: str, reply: str, tools_used: list[str], model: str | None) -> None:
        try:
            card = self.make(question, reply, model)
            if card:
                card.update(ts=datetime.now().isoformat(timespec="seconds"), tools=tools_used, question=question[:120])
                self.recent.append(card)
                if self.activity:
                    self.activity.emit("insight", **card)
        except Exception:  # una ficha nunca tumba nada
            log.exception("no se pudo hacer la ficha")
        finally:
            self._busy.release()

    def make(self, question: str, reply: str, model: str | None = None) -> dict[str, Any] | None:
        messages = [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": f"Pregunta: {question[:500]}\n\nRespuesta: {reply[:3000]}"},
        ]
        try:
            return parse_card(self.llm.chat(messages, prefer=model).text)
        except LLMError as exc:
            log.info("ficha sin hacer: %s", exc)
            return None
