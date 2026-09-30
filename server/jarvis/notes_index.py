"""Buscar en las notas por significado, no por la palabra exacta.

La busqueda normal de la boveda solo encuentra la palabra literal: "proveedor de filamento" no
encuentra una nota que diga "quien me vende el PLA". Aqui cada nota se parte en trozos (por titulos)
y cada trozo se convierte en un vector TF-IDF de dos capas:

- palabras con su raiz (plurales y terminaciones verbales fuera), que acerca "vende" y "vendedor";
- trozos de 4 letras, que aguantan erratas, acentos y palabras compuestas ("filamento"/"filamentos").

La consulta se convierte igual y se comparan por coseno. Todo en local, sin modelos ni tokens: la
boveda cabe de sobra en memoria y el indice se rehace solo cuando cambia alguna nota.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .memory.retrieval import STOPWORDS

log = logging.getLogger("jarvis.notes")

MAX_NOTES = 3000
MAX_CHUNK = 900  # letras por trozo: un titulo con su texto
MIN_SCORE = 0.06
NGRAM = 4
NGRAM_WEIGHT = 0.4
SNIPPET = 220
# Terminaciones que se quitan para quedarse con la raiz (las mas largas primero).
_SUFFIXES = ("amientos", "amiento", "imientos", "imiento", "aciones", "acion", "adores", "adora", "ador",
             "antes", "ante", "mente", "idades", "idad", "ivos", "iva", "ivo", "ados", "ado", "idos", "ido",
             "ando", "endo", "aron", "eron", "aba", "ian", "ria", "ras", "res", "los", "las", "es", "as", "os", "s")


def plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def stem(word: str) -> str:
    """Raiz aproximada: quita la terminacion y, al final, la vocal suelta ('filamento' y 'filamentos' -> 'filament')."""
    for suffix in _SUFFIXES:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    if len(word) >= 5 and word[-1] in "aeo":
        word = word[:-1]
    return word


def terms(text: str) -> list[str]:
    """Raices de las palabras con significado y trozos de 4 letras, todo junto en una bolsa."""
    words = [w for w in re.findall(r"[a-z0-9]+", plain(text)) if len(w) >= 3 and w not in STOPWORDS]
    out = [f"w:{stem(w)}" for w in words]
    joined = " ".join(words)
    out += [f"n:{joined[i:i + NGRAM]}" for i in range(max(0, len(joined) - NGRAM + 1)) if " " not in joined[i:i + NGRAM]]
    return out


@dataclass(frozen=True)
class Chunk:
    path: str
    title: str
    text: str
    vector: dict[str, float]


@dataclass(frozen=True)
class Found:
    path: str
    title: str
    score: float
    snippet: str

    def line(self) -> str:
        where = f"{self.path}{f' › {self.title}' if self.title else ''}"
        return f"- {where}: {self.snippet}"


def split_note(text: str, name: str) -> list[tuple[str, str]]:
    """La nota en trozos (titulo, texto): uno por encabezado markdown, partiendo los muy largos."""
    parts: list[tuple[str, list[str]]] = [(name, [])]
    for line in text.splitlines():
        if re.match(r"\s{0,3}#{1,6}\s+\S", line):
            parts.append((line.lstrip("# ").strip()[:120], []))
        else:
            parts[-1][1].append(line)
    out: list[tuple[str, str]] = []
    for title, lines in parts:
        body = "\n".join(lines).strip()
        if not body:
            continue
        for i in range(0, len(body), MAX_CHUNK):
            out.append((title, body[i:i + MAX_CHUNK]))
    return out


def _norm(counts: Counter, idf: dict[str, float]) -> dict[str, float]:
    vector = {t: (1 + math.log(n)) * idf.get(t, 0.0) for t, n in counts.items() if idf.get(t)}
    length = math.sqrt(sum(v * v for v in vector.values())) or 1.0
    return {t: (v / length) * (NGRAM_WEIGHT if t.startswith("n:") else 1.0) for t, v in vector.items()}


class NotesIndex:
    """Indice en memoria de la boveda. Se rehace solo si alguna nota cambia (por fecha y tamano)."""

    def __init__(self, vault, max_notes: int = MAX_NOTES):
        self.vault = vault
        self.max_notes = max_notes
        self._lock = threading.Lock()
        self._chunks: list[Chunk] = []
        self._idf: dict[str, float] = {}
        self._stamp: tuple | None = None

    def _fingerprint(self, notes: list[Path]) -> tuple:
        out = []
        for path in notes:
            try:
                st = path.stat()
            except OSError:
                continue
            out.append((path.as_posix(), int(st.st_mtime), st.st_size))
        return tuple(sorted(out))

    def refresh(self, force: bool = False) -> int:
        """Rehace el indice si hace falta; devuelve cuantos trozos tiene."""
        notes = sorted(self.vault.notes())[: self.max_notes]
        stamp = self._fingerprint(notes)
        with self._lock:
            if not force and stamp == self._stamp:
                return len(self._chunks)
            raw: list[tuple[str, str, str, Counter]] = []
            seen: Counter = Counter()
            for path in notes:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = self.vault.rel(path)
                for title, body in split_note(text, path.stem):
                    counts = Counter(terms(f"{path.stem} {title} {body}"))
                    if not counts:
                        continue
                    raw.append((rel, title, body, counts))
                    seen.update(counts.keys())
            total = len(raw) or 1
            self._idf = {t: math.log(1 + total / n) for t, n in seen.items()}
            self._chunks = [Chunk(rel, title, body, _norm(counts, self._idf)) for rel, title, body, counts in raw]
            self._stamp = stamp
            log.info("notas: índice con %d trozos de %d notas", len(self._chunks), len(notes))
            return len(self._chunks)

    def search(self, query: str, limit: int = 5) -> list[Found]:
        self.refresh()
        counts = Counter(terms(query))
        if not counts:
            return []
        with self._lock:
            vector = _norm(counts, self._idf)
            if not vector:
                return []
            scored: list[tuple[float, Chunk]] = []
            for chunk in self._chunks:
                score = sum(weight * chunk.vector.get(term, 0.0) for term, weight in vector.items())
                if score >= MIN_SCORE:
                    scored.append((score, chunk))
        scored.sort(key=lambda s: (-s[0], s[1].path))
        best: dict[str, tuple[float, Chunk]] = {}
        for score, chunk in scored:  # una entrada por nota: el mejor trozo
            if chunk.path not in best:
                best[chunk.path] = (score, chunk)
        return [Found(c.path, c.title, round(s, 3), _snippet(c.text, query)) for s, c in list(best.values())[:limit]]


def _snippet(text: str, query: str) -> str:
    """Un trozo del texto alrededor de la primera palabra de la consulta que aparezca."""
    flat = " ".join(text.split())
    words = [w for w in re.findall(r"[a-z0-9]+", plain(query)) if len(w) >= 4 and w not in STOPWORDS]
    low = plain(flat)
    at = next((low.find(stem(w)) for w in words if stem(w) in low), -1)
    if at < 0:
        return flat[:SNIPPET] + ("…" if len(flat) > SNIPPET else "")
    start = max(0, at - SNIPPET // 3)
    cut = flat[start:start + SNIPPET]
    return ("…" if start else "") + cut + ("…" if start + SNIPPET < len(flat) else "")
