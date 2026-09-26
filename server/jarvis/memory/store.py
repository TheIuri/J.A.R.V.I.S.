"""Memoria persistente (Nivel 3). SQLite es la fuente de verdad.

Reglas de la guia: separar hechos, preferencias, proyectos, decisiones y eventos;
guardar timestamp, origen y confianza; poder corregir y borrar; nunca guardar secretos.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TYPES = ("hecho", "preferencia", "proyecto", "decision", "evento")
MAX_CONTENT = 500

# Heuristicas para no guardar secretos por defecto.
_SECRET_PATTERNS = [
    re.compile(r"\b(contrase[ñn]a|password|passwd|pin|clave|token|api[ _-]?key|secreto)\b", re.I),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # numeros de tarjeta
    re.compile(r"\b(sk|gsk|ghp|xox[bp])[-_][A-Za-z0-9_-]{10,}"),  # formatos de API keys
    re.compile(r"\bES\d{2}(?:\s?\d{4}){5}\b", re.I),  # IBAN espanol
]


class MemoryRejected(ValueError):
    """Operacion de memoria rechazada (secreto, tipo invalido, id inexistente...)."""


@dataclass(frozen=True)
class Memory:
    id: int
    type: str
    content: str
    created_at: str
    updated_at: str
    source: str
    confidence: float

    def line(self) -> str:
        return f"[{self.id}] ({self.type}) {self.content}"


def looks_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class MemoryStore:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.fts = True
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence REAL NOT NULL
                )"""
            )
            try:
                self._db.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        content, content='memories', content_rowid='id',
                        tokenize='unicode61 remove_diacritics 2');
                    CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
                    END;
                    CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                        INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
                        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
                    END;
                    """
                )
            except sqlite3.OperationalError:
                self.fts = False  # SQLite sin FTS5: se busca con LIKE

    # --- escritura -------------------------------------------------------------

    def _check(self, content: str, type_: str) -> str:
        content = _norm(content)
        if not content:
            raise MemoryRejected("el recuerdo está vacío")
        if len(content) > MAX_CONTENT:
            raise MemoryRejected(f"el recuerdo es demasiado largo (máx. {MAX_CONTENT} caracteres)")
        if type_ not in TYPES:
            raise MemoryRejected(f"tipo inválido; usa uno de {TYPES}")
        if looks_secret(content):
            raise MemoryRejected("parece un dato sensible (contraseña, clave, tarjeta...); no lo guardo")
        return content

    def add(self, content: str, type_: str = "hecho", source: str = "usuario", confidence: float = 1.0) -> Memory:
        content = self._check(content, type_)
        now = _now()
        with self._lock, self._db:
            dup = self._db.execute(
                "SELECT id FROM memories WHERE lower(content) = lower(?)", (content,)
            ).fetchone()
            if dup:  # ya lo sabia: solo refresca la fecha
                self._db.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (now, dup["id"]))
                mid = dup["id"]
            else:
                mid = self._db.execute(
                    "INSERT INTO memories (type, content, created_at, updated_at, source, confidence)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (type_, content, now, now, source, max(0.0, min(1.0, confidence))),
                ).lastrowid
        return self.get(mid)

    def update(self, memory_id: int, content: str, type_: str | None = None) -> Memory:
        current = self.get(memory_id)
        content = self._check(content, type_ or current.type)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE memories SET content = ?, type = ?, updated_at = ? WHERE id = ?",
                (content, type_ or current.type, _now(), memory_id),
            )
        return self.get(memory_id)

    def delete(self, memory_id: int) -> Memory:
        memory = self.get(memory_id)
        with self._lock, self._db:
            self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return memory

    # --- lectura ---------------------------------------------------------------

    def get(self, memory_id: int) -> Memory:
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            raise MemoryRejected(f"no existe el recuerdo {memory_id}")
        return Memory(**dict(row))

    def all(self, type_: str | None = None, limit: int = 200) -> list[Memory]:
        sql, args = "SELECT * FROM memories", ()
        if type_:
            sql, args = sql + " WHERE type = ?", (type_,)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY updated_at DESC, id DESC LIMIT ?", (*args, limit)).fetchall()
        return [Memory(**dict(r)) for r in rows]

    def search(self, words: list[str], limit: int = 5, exclude_types: tuple[str, ...] = ()) -> list[Memory]:
        """Recuerdos que contienen alguna de las palabras (prefijo), los mas relevantes primero."""
        words = [w for w in words if w.isalnum()]
        if not words:
            return []
        placeholders = ",".join("?" * len(exclude_types)) or "''"
        with self._lock:
            if self.fts:
                query = " OR ".join(f"{w}*" for w in words)
                rows = self._db.execute(
                    f"SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid"
                    f" WHERE memories_fts MATCH ? AND m.type NOT IN ({placeholders})"
                    f" ORDER BY bm25(memories_fts) LIMIT ?",
                    (query, *exclude_types, limit),
                ).fetchall()
            else:
                cond = " OR ".join("lower(content) LIKE ?" for _ in words)
                rows = self._db.execute(
                    f"SELECT * FROM memories WHERE ({cond}) AND type NOT IN ({placeholders})"
                    f" ORDER BY updated_at DESC LIMIT ?",
                    (*[f"%{w.lower()}%" for w in words], *exclude_types, limit),
                ).fetchall()
        return [Memory(**dict(r)) for r in rows]

    def close(self) -> None:
        self._db.close()
