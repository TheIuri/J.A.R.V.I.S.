"""Aprender de lo que dices: tus preferencias y, sobre todo, tus correcciones.

Cuando en un turno se cuela algo que vale para siempre ("el PETG lo compro siempre en Filamentor",
"no me gustan los resúmenes largos"), se guarda como preferencia en la memoria. Luego el recuperador
las mete en todas las respuestas, así que JARVIS las aplica sin que haya que repetirlas.

Para no gastar tokens en cada turno hay un filtro previo: solo se le pregunta al modelo si la frase
suena a preferencia. Lo aprendido queda marcado (source="aprendido", confianza 0,6) y se puede
corregir o borrar como cualquier otro recuerdo.

Las correcciones son lo que mas ensena: cuando dices "no, mira tambien el calendario del trabajo" o
pones el pulgar abajo explicando que fallo, eso se convierte en una regla duradera (source="correccion",
confianza 0,8) que se le recuerda en todas las respuestas siguientes. Antes se perdia en cuanto pasaba
el turno.
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

CORRECTION_HINTS = re.compile(
    r"^\s*(no|nop|que no|pues no|te equivocas|error|mal|incorrecto|eso no|no es (?:eso|asi)|"
    r"no era eso|te has (?:equivocado|colado)|en realidad|realmente|ya te (?:dije|he dicho)|"
    r"te lo dije|otra vez|siempre te pasa|te repito)\b",
    re.I,
)

CORRECTION_PROMPT = """El usuario acaba de corregir a su asistente. Saca la REGLA duradera que hay detras,
para que no vuelva a fallar igual.
Devuelve SOLO un JSON: {"reglas": ["<una frase corta, maximo 140 caracteres, en imperativo>"]}
Escribela como una instruccion util para siempre ("Cuando pregunte por la agenda, mira tambien el calendario
del trabajo"), no como el arreglo de este caso concreto ("Di que tiene reunion a las 5").
Como mucho 1 regla. Si la correccion es de un dato puntual y no hay nada que aprender, devuelve {"reglas": []}.
Nada de contrasenas ni datos sensibles. En espanol."""

PROMPT = """Del mensaje del usuario, saca SOLO lo que sea una preferencia suya duradera: gustos, marcas o
tiendas que prefiere, como quiere que le respondan, costumbres. Nada de encargos puntuales, datos de un
momento ("manana a las 8"), preguntas ni opiniones sobre un tema concreto.
Devuelve SOLO un JSON: {"preferencias": ["<frase corta en tercera persona, maximo 120 caracteres>"]}
Escribelas como hechos sobre el usuario ("Prefiere...", "No le gusta...", "Compra el filamento en...").
Como mucho 2. Si no hay ninguna clara, devuelve {"preferencias": []}. Nada de contrasenas ni datos sensibles.
En espanol."""


def parse_rules(text: str, key: str = "preferencias") -> list[str]:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in (data.get(key) if isinstance(data, dict) else None) or []:
        clean = " ".join(str(item).split())[:120]
        if len(clean) >= MIN_CHARS:
            out.append(clean)
    return out[:MAX_NEW]


def parse_prefs(text: str) -> list[str]:
    return parse_rules(text, "preferencias")


def worth_asking(text: str) -> bool:
    return len(text.strip()) >= MIN_CHARS and bool(HINTS.search(text))


def looks_like_correction(text: str) -> bool:
    """Si la frase suena a "no, en realidad...": solo entonces se le pregunta al modelo."""
    return len(text.strip()) >= MIN_CHARS and bool(CORRECTION_HINTS.search(text.strip()))


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

    def submit_correction(self, question: str, wrong: str, correction: str, model: str | None = None) -> bool:
        """Una correccion tuya (o el pulgar abajo con el motivo) se guarda como regla. En segundo plano."""
        if not correction.strip() or not self._busy.acquire(blocking=False):
            return False
        threading.Thread(target=self._run_correction, args=(question, wrong, correction, model),
                         name="learn-fix", daemon=True).start()
        return True

    def _run_correction(self, question: str, wrong: str, correction: str, model: str | None) -> None:
        try:
            for rule in self.learn_correction(question, wrong, correction, model):
                log.info("regla aprendida de una corrección: %s", rule["content"])
                if self.activity:
                    self.activity.emit("learned", **rule)
        except Exception:
            log.exception("no se pudo aprender de la corrección")
        finally:
            self._busy.release()

    def learn_correction(self, question: str, wrong: str, correction: str,
                         model: str | None = None) -> list[dict[str, Any]]:
        """La regla que sale de la correccion, ya guardada: [{"id", "content"}]."""
        known = self.known()
        if len(known) >= MAX_PREFS:
            return []
        conversation = (f"Le preguntó: {question[:400]}\n"
                        f"El asistente contestó: {wrong[:600]}\n"
                        f"El usuario le corrigió: {correction[:400]}")
        messages = [{"role": "system", "content": CORRECTION_PROMPT}, {"role": "user", "content": conversation}]
        try:
            found = parse_rules(self.llm.chat(messages, prefer=model).text, "reglas")
        except LLMError as exc:
            log.info("sin regla que aprender: %s", exc)
            return []
        saved = []
        for rule in found[:1]:
            if not self._is_new(rule, known):
                continue
            try:
                memory = self.store.add(rule, "preferencia", source="correccion", confidence=0.8)
            except MemoryRejected as exc:
                log.info("regla descartada (%s): %s", exc, rule)
                continue
            saved.append({"id": memory.id, "content": memory.content})
        return saved

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
