"""Leads: posibles clientes que encuentra el agente captador, guardados para hacerles seguimiento.

El captador busca en internet negocios que encajen con lo que ofreces (LEADS_PROFILE) y deja al
final de su informe un bloque ```leads [...]``` que este codigo lee y guarda en leads.json.
Solo datos publicos de empresas (su web, el contacto que publican); nunca personas particulares.
Todo lo que llega de internet se guarda como texto y el HUD lo pinta como texto: un lead no
puede meter HTML ni enlaces raros (solo http/https).
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .tools.registry import Tool, ToolContext, ToolError

STATUSES = ("nuevo", "contactado", "interesado", "descartado")
MAX_LEADS = 500
FIELDS = {"nombre": ("name", 80), "tipo": ("kind", 80), "zona": ("area", 60), "web": ("web", 200),
          "contacto": ("contact", 120), "encaje": ("fit", 300), "mensaje": ("message", 600)}
_BLOCK = re.compile(r"```leads\s*(\[.*?\])\s*```", re.S)


def _plain(text: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def safe_url(url: str) -> str:
    url = url.strip()
    if re.match(r"^[\w-]+(\.[\w-]+)+(/|$)", url):  # "cafeterialuna.es" sin esquema
        url = "https://" + url
    parsed = urlparse(url)
    ok = parsed.scheme in ("http", "https") and "." in parsed.netloc and not re.search(r"[\s<>\"']", url)
    return url if ok else ""


def extract_leads(text: str) -> tuple[list[dict[str, str]], str]:
    """(leads del bloque ```leads```, el informe sin ese bloque)."""
    match = _BLOCK.search(text or "")
    if not match:
        return [], text
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        raw = []
    leads = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        lead = {key: " ".join(str(item.get(src) or "").split())[:limit] for src, (key, limit) in FIELDS.items()}
        lead["web"] = safe_url(lead["web"])
        if lead["name"]:
            leads.append(lead)
    return leads, (text[: match.start()] + text[match.end():]).strip()


class LeadStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self.leads: list[dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.leads = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.leads, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _key(lead: dict[str, Any]) -> str:
        host = urlparse(lead.get("web") or "").netloc.removeprefix("www.")
        return host or _plain(lead.get("name", ""))

    def add(self, found: list[dict[str, str]], source: str = "") -> list[dict[str, Any]]:
        """Guarda los nuevos (los repetidos por web o nombre se saltan) y los devuelve."""
        with self._lock:
            known = {self._key(lead) for lead in self.leads}
            next_id = max((lead["id"] for lead in self.leads), default=0) + 1
            new = []
            for lead in found:
                key = self._key(lead)
                if not key or key in known:
                    continue
                known.add(key)
                entry = {"id": next_id, **lead, "status": "nuevo", "found": datetime.now().isoformat(timespec="minutes"),
                         "source": source[:200], "note": ""}
                next_id += 1
                self.leads.append(entry)
                new.append(entry)
            self.leads = self.leads[-MAX_LEADS:]
            if new:
                self._save()
            return new

    def find(self, ref: str | int) -> dict[str, Any]:
        ref = str(ref).strip().lstrip("#")
        if ref.isdigit():
            for lead in self.leads:
                if lead["id"] == int(ref):
                    return lead
        key = _plain(ref)
        matches = [lead for lead in self.leads if key and key in _plain(lead["name"])]
        if not matches:
            raise ToolError(f"no encuentro ningún lead '{ref}'")
        if len(matches) > 1 and not any(_plain(m["name"]) == key for m in matches):
            raise ToolError("hay varios: " + ", ".join(f"#{m['id']} {m['name']}" for m in matches[:6]) + ". Di cuál")
        return next((m for m in matches if _plain(m["name"]) == key), matches[0])

    def update(self, ref: str | int, status: str, note: str = "") -> dict[str, Any]:
        if status not in STATUSES:
            raise ToolError(f"estado no válido: {status}; vale {', '.join(STATUSES)}")
        with self._lock:
            lead = self.find(ref)
            lead["status"] = status
            if note:
                lead["note"] = " ".join(note.split())[:300]
            self._save()
            return lead

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for lead in self.leads if lead["status"] == s) for s in STATUSES}


def lead_line(lead: dict[str, Any]) -> str:
    parts = [f"#{lead['id']} {lead['name']}", lead.get("kind"), lead.get("area"), f"[{lead['status']}]"]
    extra = " · ".join(x for x in (lead.get("web"), lead.get("contact")) if x)
    return " · ".join(p for p in parts if p) + (f" — {extra}" if extra else "")


def lead_tools(store: LeadStore) -> list[Tool]:
    def listing(_ctx: ToolContext, status: str = "", query: str = "") -> str:
        leads = [lead for lead in store.leads if (not status or lead["status"] == status)
                 and (not query or _plain(query) in _plain(" ".join(str(v) for v in lead.values())))]
        if not leads:
            return "No hay leads que coincidan." + ("" if store.leads else " Pide al captador que busque clientes.")
        counts = ", ".join(f"{n} {s}" for s, n in store.counts().items() if n)
        lines = [lead_line(lead) for lead in reversed(leads[-15:])]
        return f"{len(leads)} leads ({counts}):\n" + "\n".join(lines)

    def update(_ctx: ToolContext, lead: str, status: str, note: str = "") -> str:
        item = store.update(lead, status, note)
        return f"Hecho: {item['name']} queda como {status}."

    return [
        Tool(
            name="leads_list",
            description="Lista los leads (posibles clientes) guardados por el captador, con su estado y contacto.",
            parameters={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "query": {"type": "string", "description": "Buscar por nombre, tipo o zona"},
                },
            },
            fn=listing,
        ),
        Tool(
            name="lead_update",
            description="Cambia el estado de un lead (contactado, interesado, descartado...) y añade una nota.",
            parameters={
                "type": "object",
                "properties": {
                    "lead": {"type": "string", "description": "Nombre o número (#id) del lead"},
                    "status": {"type": "string", "enum": list(STATUSES)},
                    "note": {"type": "string"},
                },
                "required": ["lead", "status"],
            },
            fn=update,
        ),
    ]
