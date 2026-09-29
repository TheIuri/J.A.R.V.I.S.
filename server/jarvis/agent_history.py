"""Lo que ya investigaron los agentes, para no gastar tokens repitiendo un encargo parecido.

Cada trabajo terminado se guarda en el dataset (agents_history.json): el encargo, el resumen, las
tarjetas y la nota de Obsidian. Antes de lanzar un agente se busca un encargo parecido del mismo
agente que siga vigente; si lo hay, se contesta con eso y el agente no se lanza. Pedir "actualizalo"
lo lanza igual. Solo para agentes cuyo resultado envejece despacio (ver MAX_AGE_DAYS).
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# Cuanto vale lo investigado por cada agente. Los que no estan no se reutilizan nunca: el tecnico
# (el NAS cambia), el organizador y el escritor (cada encargo es distinto) y el captador (los leads
# ya evitan repetidos por su cuenta).
MAX_AGE_DAYS = {"compras": 30, "investigador": 90}
MAX_ENTRIES = 300
MIN_SHARED = 2  # palabras con significado en comun
MIN_OVERLAP = 0.6  # de las del encargo mas corto

_STOP = set("""
a al algo algun alguna algunas alguno algunos ante antes aqui asi busca buscame busque cual cuales cuanto
compara comparame comparar compra comprar con contra cosa cosas de del desde donde dos el ella en entre es esa
ese eso esta estas este esto estos haz hay investiga investigame investigar la las le les lo los mas me mejor
mejores mi mis muy necesito para pero poco por porque puedes que quiero sabes se segun ser si sin sobre su sus
tambien te tengo tiene tu tus un una unas uno unos vale y ya
""".split())


def keywords(text: str) -> set[str]:
    """Palabras con significado, sin acentos ni plurales sencillos."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    words = re.findall(r"[a-z0-9]+", plain)
    out = set()
    for w in words:
        if w in _STOP or (len(w) < 3 and not w.isdigit()):
            continue
        if len(w) > 4 and w.endswith("es"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s"):
            w = w[:-1]
        out.add(w)
    return out


def similar(a: set[str], b: set[str]) -> bool:
    shared = len(a & b)
    return shared >= MIN_SHARED and shared >= MIN_OVERLAP * min(len(a), len(b))


class AgentHistory:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.ages = dict(MAX_AGE_DAYS)  # los agentes personalizados se anaden con sus dias
        self.entries: list[dict[str, Any]] = []
        if self.path:
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.entries = []

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, agent: str, topic: str, summary: str, note: str = "", cards: list[dict] | None = None,
            model: str = "", when: datetime | None = None) -> None:
        if agent not in self.ages:
            return
        with self._lock:
            self.entries.append({
                "agent": agent, "topic": topic[:300], "summary": summary[:400], "note": note,
                "cards": cards or [], "model": model, "date": (when or datetime.now()).isoformat(timespec="minutes"),
            })
            self.entries = self.entries[-MAX_ENTRIES:]
            self._save()

    def find(self, agent: str, topic: str, now: datetime | None = None) -> dict[str, Any] | None:
        """El trabajo mas reciente del mismo agente con un encargo parecido y todavia vigente."""
        days = self.ages.get(agent)
        if not days:
            return None
        now = now or datetime.now()
        wanted = keywords(topic)
        if not wanted:
            return None
        for entry in reversed(self.entries):
            if entry["agent"] != agent:
                continue
            try:
                when = datetime.fromisoformat(entry["date"])
            except ValueError:
                continue
            if now - when.replace(tzinfo=None) > timedelta(days=days):
                continue
            if similar(wanted, keywords(entry["topic"])):
                return entry
        return None
