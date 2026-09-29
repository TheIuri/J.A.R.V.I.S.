"""Que recuerdos se inyectan antes de cada respuesta.

Primero reglas (preferencias siempre + coincidencia de palabras con FTS5). La interfaz
`Retriever` deja sitio para anadir embeddings/Qdrant cuando el volumen lo justifique.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from .store import Memory, MemoryStore

log = logging.getLogger("jarvis.memory")

STOPWORDS = set(
    """a al algo algun alguna algunas alguno algunos ante antes aqui asi aun bien cada como con contra cual
    cuales cuando de del desde donde dos el ella ellas ellos en entre era eres es esa esas ese eso esos esta
    estas este esto estos estoy fue ha haber hace hacer hasta hay la las le les lo los mas me mi mis mucho muy
    nada ni no nos nosotros o os otra otro para pero poco por porque que quien se sea ser si sin sobre solo
    son soy su sus tambien tan te tengo ti tiene tu tus un una uno unos usted vez y ya yo dime dame puedes
    quiero favor hola gracias sabes recuerdas acuerdas jarvis""".split()
)


@dataclass(frozen=True)
class Recalled:
    memory: Memory
    reason: str


class Retriever(Protocol):
    store: MemoryStore

    def recall(self, text: str) -> list[Recalled]: ...


def keywords(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9ñ]+", plain)
    return list(dict.fromkeys(w for w in words if len(w) >= 3 and w not in STOPWORDS))


class RuleRetriever:
    def __init__(self, store: MemoryStore, max_items: int = 14, max_preferences: int = 8):
        self.store = store
        self.max_items = max_items
        self.max_preferences = max_preferences

    def recall(self, text: str) -> list[Recalled]:
        picked: dict[int, Recalled] = {}
        # 1) Preferencias: siempre, porque cambian como responder a casi todo.
        for m in self.store.all("preferencia", limit=self.max_preferences):
            picked[m.id] = Recalled(m, "preferencia (siempre)")
        # 2) El resto, por palabras en comun con lo que acaba de decir el usuario.
        words = keywords(text)
        room = self.max_items - len(picked)
        if words and room > 0:
            for m in self.store.search(words, limit=room + len(picked)):
                if m.id not in picked and len(picked) < self.max_items:
                    hits = [w for w in words if w in keywords(m.content) or any(k.startswith(w) for k in keywords(m.content))]
                    picked[m.id] = Recalled(m, f"coincide: {', '.join(hits) or 'palabras'}")
        result = list(picked.values())
        for r in result:
            log.info("recuerdo %d seleccionado (%s): %s", r.memory.id, r.reason, r.memory.content[:80])
        return result


def as_prompt(recalled: list[Recalled]) -> str:
    if not recalled:
        return ""
    lines = "\n".join(f"- {r.memory.line()}" for r in recalled)
    return (
        "\n\nLo que recuerdas del usuario (memoria; el numero entre corchetes es su id):\n"
        f"{lines}\n"
        "Usalo solo si es relevante. Si algo esta desactualizado, corrigelo con memory_update."
    )
