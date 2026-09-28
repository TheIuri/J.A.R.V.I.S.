"""Bóveda de Obsidian: carpeta de ficheros Markdown montada en el contenedor.

Reglas: nunca se borra ni se sobrescribe una nota (solo crear y añadir), nada fuera de la
bóveda, se ignoran las carpetas ocultas (.obsidian, .trash) y no se guardan secretos.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .memory import MemoryStore, keywords, looks_secret

log = logging.getLogger(__name__)

MAX_READ_CHARS = 4000
MAX_WRITE_CHARS = 4000
MEMORY_NOTE = "JARVIS/Memoria.md"
_BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]]')


class VaultError(ValueError):
    """Operación rechazada (ruta fuera de la bóveda, nota inexistente, secreto...)."""


@dataclass(frozen=True)
class Hit:
    path: str
    score: int
    snippet: str


class Vault:
    def __init__(self, root: str | Path, timezone: str = "Europe/Madrid", inbox: str = "Inbox", daily: str = "Diario"):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise VaultError(f"la bóveda {self.root} no existe o no es una carpeta")
        self.tz = ZoneInfo(timezone)
        self.inbox = inbox.strip("/")
        self.daily = daily.strip("/")
        self._lock = threading.Lock()

    # --- rutas ------------------------------------------------------------------

    def _resolve(self, rel: str, must_exist: bool = True) -> Path:
        rel = rel.strip().lstrip("/")
        if not rel.lower().endswith(".md"):
            rel += ".md"
        path = (self.root / rel).resolve()
        if self.root not in path.parents:
            raise VaultError("ruta fuera de la bóveda")
        if any(part.startswith(".") for part in path.relative_to(self.root).parts):
            raise VaultError("no se puede acceder a carpetas ocultas de la bóveda")
        if must_exist and not path.is_file():
            raise VaultError(f"no existe la nota '{rel}'")
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def notes(self) -> list[Path]:
        found = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            found += [Path(dirpath) / f for f in filenames if f.lower().endswith(".md") and not f.startswith(".")]
        return found

    # --- lectura ----------------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> list[Hit]:
        words = keywords(query)
        if not words:
            return []
        hits = []
        for path in self.notes():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            name = " ".join(keywords(path.stem))
            body = " ".join(keywords(text))
            score = sum(3 * name.count(w) + body.count(w) for w in words)
            if score:
                hits.append(Hit(self.rel(path), score, _snippet(text, words)))
        hits.sort(key=lambda h: (-h.score, h.path))
        return hits[:limit]

    def read(self, rel: str) -> str:
        text = self._resolve(rel).read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_READ_CHARS:
            return text[:MAX_READ_CHARS] + f"\n[... nota recortada: {len(text)} caracteres en total]"
        return text

    # --- escritura (solo crear y añadir) ---------------------------------------

    def _check_text(self, text: str, check_secrets: bool = True, max_chars: int = MAX_WRITE_CHARS) -> str:
        text = text.strip()
        if not text:
            raise VaultError("el texto está vacío")
        if len(text) > max_chars:
            raise VaultError(f"texto demasiado largo (máx. {max_chars} caracteres)")
        if check_secrets and looks_secret(text):
            raise VaultError("parece un dato sensible (contraseña, clave, tarjeta...); no lo escribo")
        return text

    def create(
        self, title: str, content: str, folder: str = "", check_secrets: bool = True, max_chars: int = MAX_WRITE_CHARS
    ) -> str:
        """check_secrets=False y max_chars mayor solo para informes de agentes (no los dicta el usuario)."""
        content = self._check_text(content, check_secrets, max_chars)
        name = _BAD_NAME.sub("", title).strip().strip(".")
        if not name:
            raise VaultError("título no válido")
        folder = (folder or self.inbox).strip("/")
        with self._lock:
            path = self._resolve(f"{folder}/{name}", must_exist=False)
            n = 2
            while path.exists():  # nunca sobrescribir
                path = self._resolve(f"{folder}/{name} ({n})", must_exist=False)
                n += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{content}\n", encoding="utf-8")
        return self.rel(path)

    def append(self, rel: str, text: str) -> str:
        text = self._check_text(text)
        with self._lock:
            path = self._resolve(rel)
            _append(path, text)
        return self.rel(path)

    def append_daily(self, text: str) -> str:
        text = self._check_text(text)
        now = datetime.now(self.tz)
        with self._lock:
            path = self._resolve(f"{self.daily}/{now:%Y-%m-%d}", must_exist=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            _append(path, f"- {now:%H:%M} {text}")
        return self.rel(path)

    def append_daily_block(self, title: str, text: str) -> str:
        """Anade una seccion a la nota del dia. Quita las lineas que parezcan secretos."""
        lines = [line for line in text.strip().splitlines() if not looks_secret(line)]
        block = self._check_text(f"## {title}\n" + "\n".join(lines), check_secrets=False)
        now = datetime.now(self.tz)
        with self._lock:
            path = self._resolve(f"{self.daily}/{now:%Y-%m-%d}", must_exist=False)
            path.parent.mkdir(parents=True, exist_ok=True)
            _append(path, "\n" + block)
        return self.rel(path)

    # --- memoria como nota de solo lectura ----------------------------------------

    def export_memory(self, store: MemoryStore) -> None:
        memories = store.all(limit=1000)
        lines = [
            "---",
            "generado_por: JARVIS",
            f"actualizado: {datetime.now(self.tz):%Y-%m-%d %H:%M}",
            "---",
            "# Lo que JARVIS recuerda de ti",
            "",
            "> Nota generada automáticamente: los cambios aquí se pierden.",
            "> Para corregir o borrar algo, díselo a JARVIS o usa /memoria.",
            "",
        ]
        for type_ in ("preferencia", "hecho", "proyecto", "decision", "evento"):
            group = [m for m in memories if m.type == type_]
            if group:
                lines.append(f"## {type_.capitalize()}")
                lines += [f"- {m.content} `#{m.id}`" for m in group]
                lines.append("")
        if not memories:
            lines.append("_Todavía no recuerdo nada._")
        path = self._resolve(MEMORY_NOTE, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, "\n".join(lines) + "\n")


def _snippet(text: str, words: list[str], width: int = 160) -> str:
    plain = " ".join(text.split())
    low = plain.lower()
    pos = min((i for w in words if (i := low.find(w)) >= 0), default=0)
    start = max(0, pos - width // 3)
    return ("…" if start else "") + plain[start : start + width] + ("…" if start + width < len(plain) else "")


def _append(path: Path, text: str) -> None:
    prefix = ""
    if path.exists() and path.stat().st_size:
        with path.open("rb") as fh:
            fh.seek(-1, os.SEEK_END)
            prefix = "" if fh.read(1) == b"\n" else "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{prefix}{text}\n")


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".jarvis-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
