"""Vigilancia de alucinaciones: cada dato concreto de un informe se comprueba contra lo que el agente leyo.

Sin LLM (no gasta tokens): se extraen las webs, emails, telefonos y precios del informe, las tarjetas y los leads, y
se busca cada uno en el texto que devolvieron las herramientas (busquedas y paginas leidas). Lo que no aparece se
marca "sin verificar": puede ser correcto, pero el agente no lo vio en ninguna fuente.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

MAX_EVIDENCE = 400_000  # caracteres de fuentes que se guardan por encargo

_URL = re.compile(r"https?://[^\s)\]>\"'`]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Telefonos de Espana: 612 345 678 · 612 34 56 78 · 93 000 00 00 · +34 612345678
_PHONE = re.compile(
    r"(?<![\w.,/])(?:\+34[\s.-]?)?[6789](?:\d{2}[\s.-]?\d{3}[\s.-]?\d{3}|\d{2}(?:[\s.-]?\d{2}){3}"
    r"|\d[\s.-]?\d{3}(?:[\s.-]?\d{2}){2})(?![\w])"
)
_PRICE = re.compile(r"(?<![\w.,/])(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s?(?:€|eur(?:os)?\b)", re.I)


def _domain(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _price_forms(raw: str) -> set[str]:
    """ "1.299,00" -> {"1299", "1.299", "1 299", "1299,00"...}: formas en que puede aparecer en la fuente."""
    integer = re.split(r"[,]", raw.strip())[0]
    plain = re.sub(r"[.\s]", "", integer)
    if "." in raw and "," not in raw and len(raw.split(".")[-1]) <= 2:  # 12.99 (decimal con punto)
        plain = raw.split(".")[0]
    forms = {plain}
    if len(plain) > 3:
        forms |= {f"{plain[:-3]}.{plain[-3:]}", f"{plain[:-3]} {plain[-3:]}", f"{plain[:-3]},{plain[-3:]}"}
    return forms


class Evidence:
    """Lo que el agente leyo, preparado para buscar datos rapido."""

    def __init__(self, texts: list[str]):
        text = "\n".join(texts)[:MAX_EVIDENCE]
        self.lower = text.lower()
        self.digits = _digits(text)
        self.domains = {_domain(u) for u in _URL.findall(text)}

    def has_url(self, url: str) -> bool:
        d = _domain(url)
        return bool(d) and (d in self.domains or d in self.lower)

    def has_email(self, email: str) -> bool:
        return email.lower() in self.lower

    def has_phone(self, phone: str) -> bool:
        digits = _digits(phone)[-9:]
        return len(digits) == 9 and digits in self.digits

    def has_price(self, raw: str) -> bool:
        return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d])", self.lower) for f in _price_forms(raw))


def facts(text: str) -> list[tuple[str, str]]:
    """Datos concretos de un texto: (tipo, valor)."""
    out: list[tuple[str, str]] = []
    for url in _URL.findall(text or ""):
        out.append(("web", url.rstrip(".,;:")))
    for email in _EMAIL.findall(text or ""):
        out.append(("email", email.rstrip(".")))
    for phone in _PHONE.findall(text or ""):
        out.append(("teléfono", phone.strip()))
    for price in _PRICE.findall(text or ""):
        out.append(("precio", price.strip()))
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _ok(ev: Evidence, kind: str, value: str) -> bool:
    return {"web": ev.has_url, "email": ev.has_email, "teléfono": ev.has_phone, "precio": ev.has_price}[kind](value)


def check(ev: Evidence, text: str) -> list[tuple[str, str]]:
    """Los datos de `text` que no aparecen en ninguna fuente."""
    return [(k, v) for k, v in facts(text) if not _ok(ev, k, v)]


def verify(report: str, cards: list[dict[str, Any]], leads: list[dict[str, Any]], evidence: list[str]) -> dict:
    """Marca tarjetas y leads (campo "unverified") y devuelve el resumen para el informe y el aviso."""
    if not evidence:
        return {"checked": 0, "unverified": [], "skipped": True}  # sin fuentes guardadas no se puede juzgar
    ev = Evidence(evidence)
    for card in cards:
        card["unverified"] = [f"{k}: {v}" for k, v in check(ev, " ".join(
            str(card.get(f, "")) for f in ("url", "price", "data")))]
    for lead in leads:
        lead["unverified"] = [f"{k}: {v}" for k, v in check(ev, " ".join(
            str(lead.get(f, "")) for f in ("web", "contact", "contacto")))]
    all_facts = facts(report)
    missing = [(k, v) for k, v in all_facts if not _ok(ev, k, v)]
    return {"checked": len(all_facts), "unverified": [f"{k}: {v}" for k, v in missing], "skipped": False}


def section(result: dict) -> str:
    """Seccion para la nota de Obsidian."""
    if result.get("skipped"):
        return ""
    if not result["unverified"]:
        return f"\n\n## Verificación de datos\nComprobados {result['checked']} datos: todos aparecen en las fuentes leídas."
    items = "\n".join(f"- {u}" for u in result["unverified"][:30])
    return (f"\n\n## Verificación de datos\nComprobados {result['checked']} datos. Estos **no aparecen en ninguna fuente "
            f"que leyó el agente** (pueden ser inventados; compruébalos antes de usarlos):\n{items}")
