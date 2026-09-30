"""Aprender las preferencias del usuario de lo que va diciendo.

Cuando en un turno se cuela algo que vale para siempre ("el PETG lo compro siempre en Filamentor",
"no me gustan los resúmenes largos"), se guarda como preferencia en la memoria. Luego el recuperador
las mete en todas las respuestas, así que JARVIS las aplica sin que haya que repetirlas.

Para no gastar tokens en cada turno hay un filtro previo: solo se le pregunta al modelo si la frase
suena a preferencia. Lo aprendido queda marcado (source="aprendido", confianza 0,6) y se puede
corregir o borrar como cualquier otro recuerdo.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from .activity import ActivityLog
from .llm import FallbackLLM, LLMError
from .memory import MemoryRejected, MemoryStore, keywords

log = logging.getLogger("jarvis.learn")

MAX_NEW = 2  # preferencias nuevas por turno
MIN_CHARS = 12
MAX_PREFS = 60  # a partir de aqui no se aprende mas solo: que el usuario limpie
SIMILAR = 0.7  # solapamiento de palabras para considerar que ya se sabe

# Solo se le pregunta al modelo si aparece algo de esto: si no, el turno no suena a preferencia.
HINTS = re.compile(
    r"\b(prefier[oa]|me gustan?|no me gustan?|odio|detesto|suelo|siempre|nunca|mejor\s+(?:que|con)|"
    r"a partir de ahora|de ahora en adelante|recuerda que|ten en cuenta|no vuelvas|deja de|"
    r"me molesta|no quiero que|quiero que siempre|mi favorit[oa]|mis favorit[oa]s|normalmente|"
    r"por defecto|acuerdate de que|no soporto|evita)\b",
    re.I,
)

PROMPT = """Del mensaje del usuario, saca SOLO lo que sea una preferencia suya duradera: gustos, marcas o
tiendas que prefiere, como quiere que le respondan, costumbres. Nada de encargos puntuales, datos de un
momento ("manana a las 8"), preguntas ni opiniones sobre un tema concreto.
Devuelve SOLO un JSON: {"preferencias": ["<frase corta en tercera persona, maximo 120 caracteres>"]}
Escribelas como hechos sobre el usuario ("Prefiere...", "No le gusta...", "Compra el filamento en...").
Como mucho 2. Si no hay ninguna clara, devuelve {"preferencias": []}. Nada de contrasenas ni datos sensibles.
En espanol."""


def parse_prefs(text: str) -> list[str]:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in (data.get("preferencias") if isinstance(data, dict) else None) or []:
        clean = " ".join(str(item).split())[:120]
        if len(clean) >= MIN_CHARS:
            out.append(clean)
    return out[:MAX_NEW]


def worth_asking(text: str) -> bool:
    return len(text.strip()) >= MIN_CHARS and bool(HINTS.search(text))


class PrefLearner:
    def __init__(self, llm: FallbackLLM, store: MemoryStore, activity: ActivityLog | None = None):
        self.llm = llm
        self.store = store
        self.activity = activity
        self._busy = threading.Lock()

    def known(self) -> list[str]:
        return [m.content for m in self.store.all("preferencia", limit=MAX_PREFS)]

    def _is_new(self, text: str, known: list[str]) -> bool:
        words = set(keywords(text))
        if not words:
            return False
        for old in known:
            other = set(keywords(old))
            shared = len(words & other)
            if other and shared >= SIMILAR * min(len(words), len(other)):
                return False
        return True

    def submit(self, text: str, model: str | None = None) -> bool:
        """En segundo plano, por si el modelo tarda. Devuelve si se ha puesto a ello."""
        if not worth_asking(text) or not self._busy.acquire(blocking=False):
            return False
        threading.Thread(target=self._run, args=(text, model), name="learn", daemon=True).start()
        return True

    def _run(self, text: str, model: str | None) -> None:
        try:
            for pref in self.learn(text, model):
                log.info("preferencia aprendida: %s", pref["content"])
                if self.activity:
                    self.activity.emit("learned", **pref)
        except Exception:  # aprender nunca puede tumbar un turno
            log.exception("no se pudo aprender la preferencia")
        finally:
            self._busy.release()

    def learn(self, text: str, model: str | None = None) -> list[dict[str, Any]]:
        """Lo que se ha guardado de este mensaje: [{"id", "content"}]."""
        known = self.known()
        if len(known) >= MAX_PREFS:
            return []
        messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": text[:1200]}]
        try:
            found = parse_prefs(self.llm.chat(messages, prefer=model).text)
        except LLMError as exc:
            log.info("sin aprender nada: %s", exc)
            return []
        saved = []
        for pref in found:
            if not self._is_new(pref, known + [s["content"] for s in saved]):
                continue
            try:
                memory = self.store.add(pref, "preferencia", source="aprendido", confidence=0.6)
            except MemoryRejected as exc:  # secreto, demasiado largo...
                log.info("preferencia descartada (%s): %s", exc, pref)
                continue
            saved.append({"id": memory.id, "content": memory.content})
        return saved
