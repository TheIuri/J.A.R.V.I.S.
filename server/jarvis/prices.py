"""Precios: guarda lo que ven los agentes y avisa cuando algo baja.

De cada informe de un agente de compras (o de cualquiera que traiga una tabla con precios) se sacan
los pares producto-precio-enlace y se guardan en SQLite con la fecha. Con eso hay histórico: se puede
dibujar la evolución de cada producto en el HUD y avisar cuando baja de un objetivo o cae un tanto por
ciento respecto a lo más barato visto.

Una "vigilancia" es un encargo guardado (agente + texto) que se repite cada cierto tiempo. El vigilante
de precios (watch.prices_check) relanza los que tocan; lo que vuelva se guarda aquí solo.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("jarvis.prices")

MAX_ITEMS = 12  # productos por informe
MAX_NAME = 80
MIN_CENTS = 1
MAX_CENTS = 100_000_00  # 100.000 €: por encima seguro que no es un precio
DROP_PCT = 3.0  # baja mínima para avisar sin objetivo
MAX_WATCHES = 12
MIN_HOURS = 6
KEEP_DAYS = 400

_MONEY = re.compile(r"(?:(?P<sym1>[€$£])\s*)?(?P<num>\d[\d.,\s]{0,13}\d|\d)\s*(?P<sym2>€|eur(?:os)?|\$|£)?", re.I)
_URL = re.compile(r"https?://[^\s)\]|<>\"']+")
_NO = ("no visto", "no encontrado", "sin precio", "n/d", "n/a", "-", "—", "?")


def plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def slug(name: str) -> str:
    """Clave estable de un producto: sin acentos, sin símbolos y sin palabras de relleno."""
    words = re.findall(r"[a-z0-9]+", plain(name))
    words = [w for w in words if w not in ("el", "la", "los", "las", "de", "del", "con", "para", "y")]
    return " ".join(words)[:MAX_NAME]


def parse_price(text: str) -> int | None:
    """'1.299,00 €' -> 129900 céntimos. None si no hay un precio claro ('no visto', texto suelto...)."""
    raw = " ".join(str(text or "").split())
    if not raw or plain(raw).strip(" .") in _NO:
        return None
    match = _MONEY.search(raw)
    if not match:
        return None
    if not (match.group("sym1") or match.group("sym2")) and not re.search(r"\d[.,]\d", raw):
        return None  # un número suelto sin moneda ni decimales no es un precio ("16 GB", "2024")
    cents = _to_cents(match.group("num"))
    if cents is None:
        return None
    return cents if MIN_CENTS <= cents <= MAX_CENTS else None


def _to_cents(num: str) -> int | None:
    """'1.299,00' y '1,299.00' son lo mismo: el ultimo separador manda si le siguen 1 o 2 cifras."""
    num = num.replace(" ", "")
    last = max(num.rfind(","), num.rfind("."))
    if last >= 0 and len(num) - last - 1 in (1, 2):  # ...,50 o ....5: es la parte decimal
        whole, dec = num[:last], num[last + 1:]
    else:  # solo separadores de millar (1.299, 1,299) o ninguno
        whole, dec = num, ""
    whole = re.sub(r"[.,]", "", whole)
    if not whole.isdigit() or (dec and not dec.isdigit()):
        return None
    return int(whole) * 100 + round(float(f"0.{dec}") * 100 if dec else 0)


def euros(cents: int) -> str:
    return f"{cents / 100:,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".") + " €"


def _cell_url(cell: str) -> str:
    match = _URL.search(cell or "")
    return match.group(0).rstrip(".,;") if match else ""


def from_table(report: str) -> list[dict[str, Any]]:
    """Productos de las tablas markdown del informe (columnas 'precio' y, si está, 'enlace')."""
    items: list[dict[str, Any]] = []
    rows = [line for line in (report or "").splitlines() if line.strip().startswith("|")]
    head: list[str] = []
    price_col = link_col = -1
    for line in rows:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):  # la línea separadora de la cabecera
            continue
        low = [plain(c) for c in cells]
        if any("precio" in c or "coste" in c for c in low):  # cabecera nueva
            head = cells
            price_col = next(i for i, c in enumerate(low) if "precio" in c or "coste" in c)
            link_col = next((i for i, c in enumerate(low) if "enlace" in c or "url" in c or "link" in c), -1)
            continue
        if not head or price_col >= len(cells):
            continue
        name = re.sub(r"[*`\[\]]", "", cells[0]).strip()
        price = parse_price(cells[price_col])
        if not name or price is None or not slug(name):
            continue
        url = _cell_url(cells[link_col]) if 0 <= link_col < len(cells) else _cell_url(line)
        items.append({"name": name[:MAX_NAME], "cents": price, "url": url})
    return items


def from_cards(cards: list[dict]) -> list[dict[str, Any]]:
    """Productos de las tarjetas de opciones del agente (bloque ```opciones```)."""
    out = []
    for card in cards or []:
        price = parse_price(card.get("price", ""))
        name = " ".join(str(card.get("title", "")).split())
        if name and price is not None and slug(name):
            out.append({"name": name[:MAX_NAME], "cents": price, "url": str(card.get("url", ""))})
    return out


def from_report(report: str, cards: list[dict] | None = None) -> list[dict[str, Any]]:
    """Lo que se puede guardar de un informe: tarjetas primero (más fiables) y luego las tablas."""
    seen: dict[str, dict] = {}
    for item in from_cards(cards or []) + from_table(report):
        key = slug(item["name"])
        if key not in seen or (not seen[key]["url"] and item["url"]):
            seen[key] = item
    return list(seen.values())[:MAX_ITEMS]


@dataclass(frozen=True)
class Drop:
    product: str
    cents: int
    before: int
    url: str
    target: int = 0

    @property
    def pct(self) -> float:
        return 100.0 * (self.before - self.cents) / self.before if self.before else 0.0

    def text(self) -> str:
        why = f" (tu objetivo era {euros(self.target)})" if self.target else f", un {self.pct:.0f}% menos"
        return f"{self.product}: {euros(self.cents)}{why}. Antes {euros(self.before)}."


@dataclass(frozen=True)
class Watch:
    id: int
    agent: str
    task: str
    product: str
    target: int
    every_hours: int
    last_run: str

    def line(self) -> str:
        target = f" hasta {euros(self.target)}" if self.target else ""
        return (f"[{self.id}] {self.task} · {self.agent}{target}, cada {self.every_hours} h"
                + (f" (última vez {self.last_run[:16].replace('T', ' ')})" if self.last_run else " (aún no ha ido)"))


class PriceStore:
    def __init__(self, path: Path | str, timezone: str = "Europe/Madrid"):
        self.tz = ZoneInfo(timezone)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS prices (
                    id INTEGER PRIMARY KEY, slug TEXT NOT NULL, product TEXT NOT NULL, cents INTEGER NOT NULL,
                    url TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL DEFAULT '', seen_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS prices_slug ON prices (slug, seen_at);
                CREATE TABLE IF NOT EXISTS watches (
                    id INTEGER PRIMARY KEY, agent TEXT NOT NULL, task TEXT NOT NULL, product TEXT NOT NULL DEFAULT '',
                    target INTEGER NOT NULL DEFAULT 0, every_hours INTEGER NOT NULL DEFAULT 24,
                    created TEXT NOT NULL, last_run TEXT NOT NULL DEFAULT '');
                """
            )

    # --- histórico ---------------------------------------------------------------

    def best(self, key: str) -> sqlite3.Row | None:
        """Lo más barato visto de un producto."""
        with self._lock:
            return self._db.execute(
                "SELECT * FROM prices WHERE slug = ? ORDER BY cents, seen_at DESC LIMIT 1", (key,)
            ).fetchone()

    def record(self, items: list[dict], agent: str = "", now: datetime | None = None) -> list[Drop]:
        """Guarda lo visto y devuelve las bajadas que merecen aviso."""
        now = now or datetime.now(self.tz)
        stamp = now.isoformat(timespec="seconds")
        drops: list[Drop] = []
        targets = {w.product: w.target for w in self.watches() if w.product and w.target}
        for item in items[:MAX_ITEMS]:
            key = slug(item.get("name", ""))
            cents = item.get("cents")
            if not key or not isinstance(cents, int):
                continue
            before = self.best(key)
            with self._lock, self._db:
                self._db.execute(
                    "INSERT INTO prices (slug, product, cents, url, agent, seen_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (key, str(item.get("name", ""))[:MAX_NAME], cents, str(item.get("url", ""))[:300], agent, stamp),
                )
            target = next((t for p, t in targets.items() if p and (p in key or key in p)), 0)
            if before is None:
                if target and cents <= target:  # ya nace por debajo del objetivo
                    drops.append(Drop(item["name"], cents, cents, item.get("url", ""), target))
                continue
            if target and cents <= target < before["cents"]:
                drops.append(Drop(item["name"], cents, before["cents"], item.get("url", ""), target))
            elif not target and cents < before["cents"] and 100.0 * (before["cents"] - cents) / before["cents"] >= DROP_PCT:
                drops.append(Drop(item["name"], cents, before["cents"], item.get("url", "")))
        if drops:
            log.info("precios: %d bajada(s): %s", len(drops), "; ".join(d.product for d in drops))
        return drops

    def record_report(self, report: str, cards: list[dict] | None = None, agent: str = "") -> list[Drop]:
        return self.record(from_report(report, cards), agent)

    def history(self, product: str, days: int = 180) -> list[dict]:
        """Evolución de un producto: un punto por día (el más barato de ese día)."""
        key = slug(product)
        since = (datetime.now(self.tz) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock:
            rows = self._db.execute(
                "SELECT substr(seen_at, 1, 10) AS day, MIN(cents) AS cents, product, url FROM prices"
                " WHERE (slug = ? OR slug LIKE ?) AND seen_at >= ? GROUP BY day ORDER BY day",
                (key, f"%{key}%", since),
            ).fetchall()
        return [{"date": r["day"], "cents": r["cents"], "price": euros(r["cents"]), "product": r["product"],
                 "url": r["url"]} for r in rows]

    def products(self, limit: int = 40) -> list[dict]:
        """Lo vigilado: último precio, el mejor visto y cuántas veces se ha mirado."""
        with self._lock:
            rows = self._db.execute(
                "SELECT slug, MAX(seen_at) AS last, COUNT(*) AS n, MIN(cents) AS best FROM prices"
                " GROUP BY slug ORDER BY last DESC LIMIT ?", (limit,)
            ).fetchall()
            out = []
            for row in rows:
                last = self._db.execute(
                    "SELECT product, cents, url FROM prices WHERE slug = ? ORDER BY seen_at DESC LIMIT 1", (row["slug"],)
                ).fetchone()
                out.append({"product": last["product"], "slug": row["slug"], "cents": last["cents"],
                            "price": euros(last["cents"]), "best": euros(row["best"]), "best_cents": row["best"],
                            "url": last["url"], "points": row["n"], "last": row["last"]})
        return out

    def clean(self, days: int = KEEP_DAYS) -> int:
        old = (datetime.now(self.tz) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock, self._db:
            return self._db.execute("DELETE FROM prices WHERE seen_at < ?", (old,)).rowcount

    # --- vigilancias --------------------------------------------------------------

    def watches(self) -> list[Watch]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM watches ORDER BY id").fetchall()
        return [Watch(r["id"], r["agent"], r["task"], r["product"], r["target"], r["every_hours"], r["last_run"])
                for r in rows]

    def add_watch(self, agent: str, task: str, product: str = "", target_cents: int = 0, every_hours: int = 24) -> Watch:
        task = " ".join(task.split())[:300]
        if not task:
            raise ValueError("¿qué quieres vigilar?")
        every_hours = max(MIN_HOURS, min(24 * 14, int(every_hours)))
        with self._lock, self._db:
            if len(self._db.execute("SELECT id FROM watches").fetchall()) >= MAX_WATCHES:
                raise ValueError(f"ya hay {MAX_WATCHES} vigilancias de precio; quita alguna antes")
            cur = self._db.execute(
                "INSERT INTO watches (agent, task, product, target, every_hours, created) VALUES (?, ?, ?, ?, ?, ?)",
                (agent, task, slug(product), max(0, int(target_cents)), every_hours,
                 datetime.now(self.tz).isoformat(timespec="seconds")),
            )
        return Watch(cur.lastrowid, agent, task, slug(product), max(0, int(target_cents)), every_hours, "")

    def remove_watch(self, watch_id: int) -> Watch:
        match = next((w for w in self.watches() if w.id == watch_id), None)
        if match is None:
            raise ValueError(f"no hay ninguna vigilancia con id {watch_id}")
        with self._lock, self._db:
            self._db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        return match

    def due(self, now: datetime | None = None) -> list[Watch]:
        """Vigilancias a las que les toca (y las marca como lanzadas)."""
        now = now or datetime.now(self.tz)
        ready = []
        for watch in self.watches():
            if watch.last_run:
                try:
                    last = datetime.fromisoformat(watch.last_run)
                except ValueError:
                    last = None
                if last and now - last < timedelta(hours=watch.every_hours):
                    continue
            ready.append(watch)
        with self._lock, self._db:
            for watch in ready:
                self._db.execute("UPDATE watches SET last_run = ? WHERE id = ?",
                                 (now.isoformat(timespec="seconds"), watch.id))
        return ready

    def close(self) -> None:
        self._db.close()


def prices_card(store: PriceStore, products: list[dict], drops: list[Drop] | None = None,
                title: str = "Precios") -> dict[str, Any]:
    """Tarjeta para el HUD: cada producto con su precio, su mejor precio y su serie para la gráfica."""
    items = []
    for prod in products[:8]:
        series = store.history(prod["slug"] if "slug" in prod else prod["product"])
        items.append({
            "product": prod["product"], "price": prod["price"], "best": prod.get("best", ""),
            "url": prod.get("url", "") if str(prod.get("url", "")).startswith("https://") else "",
            "series": [{"date": p["date"], "cents": p["cents"]} for p in series[-30:]],
            "down": bool(series) and prod.get("cents", 0) <= (prod.get("best_cents") or 0),
        })
    return {"kind": "prices", "title": title, "items": items,
            "drops": [{"product": d.product, "price": euros(d.cents), "before": euros(d.before),
                       "pct": round(d.pct), "url": d.url if d.url.startswith("https://") else ""} for d in (drops or [])]}
