"""Tools de informacion sin cuentas: busqueda web, Wikipedia, noticias, unidades y divisas.

Todo gratis y sin API key (DuckDuckGo, Wikipedia, Google News RSS, Frankfurter/BCE). Si se
configura BRAVE_API_KEY, la busqueda web usa Brave, que es mas estable que leer el HTML de DuckDuckGo.
Los resultados se recortan: al modelo le basta con titulos y fragmentos.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlparse

import httpx

from .registry import Tool, ToolContext, ToolError

UA = "Mozilla/5.0 (JARVIS asistente personal)"
MAX_SNIPPET = 220


def _clean(text: str, limit: int = MAX_SNIPPET) -> str:
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", "", text or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- busqueda web -------------------------------------------------------------------


class _DuckParser(HTMLParser):
    """Lee los resultados de html.duckduckgo.com (titulo, enlace y fragmento; sin anuncios)."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._field: str | None = None
        self._ad = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div" and "result" in cls.split():
            self._ad = "result--ad" in cls  # anuncio: se ignora
        if tag == "a" and "result__a" in cls and not self._ad:
            self.results.append({"title": "", "url": _duck_url(a.get("href", "")), "snippet": ""})
            self._field = "title"
        elif tag in ("a", "div") and "result__snippet" in cls and self.results and not self._ad:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "div"):
            self._field = None

    def handle_data(self, data):
        if self._field and self.results:
            self.results[-1][self._field] += data


def _duck_url(href: str) -> str:
    # Los enlaces pasan por //duckduckgo.com/l/?uddg=<url real>
    if "uddg=" in href:
        return parse_qs(urlparse(href).query).get("uddg", [href])[0]
    return href


class WebSearch:
    def __init__(self, brave_key: str = "", client: httpx.Client | None = None):
        self.brave_key = brave_key
        self._client = client or httpx.Client(timeout=10, headers={"User-Agent": UA}, follow_redirects=True)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        try:
            results = self._brave(query) if self.brave_key else self._duck(query)
        except httpx.HTTPError as exc:
            raise ToolError(f"el buscador no responde ({type(exc).__name__})") from exc
        return [
            {"title": _clean(r["title"], 120), "url": r["url"], "snippet": _clean(r["snippet"])}
            for r in results
            if r.get("title") and r.get("url", "").startswith("http")
        ][:limit]

    def _duck(self, query: str) -> list[dict]:
        resp = self._client.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "es-es"})
        resp.raise_for_status()
        parser = _DuckParser()
        parser.feed(resp.text)
        if not parser.results and "anomaly" in resp.text.lower():
            raise ToolError("DuckDuckGo ha bloqueado la busqueda temporalmente; prueba en un rato")
        return parser.results

    def _brave(self, query: str) -> list[dict]:
        resp = self._client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "country": "es", "search_lang": "es", "count": 8},
            headers={"X-Subscription-Token": self.brave_key, "Accept": "application/json"},
        )
        resp.raise_for_status()
        return [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in resp.json().get("web", {}).get("results", [])
        ]


def web_search_tool(search: WebSearch) -> Tool:
    def run(_ctx: ToolContext, query: str, max_results: int = 5) -> str:
        results = search.search(query, max_results)
        if not results:
            return f"Sin resultados para '{query}'."
        return "\n".join(f"{i}. {r['title']} — {r['snippet']} ({r['url']})" for i, r in enumerate(results, 1))

    return Tool(
        name="web_search",
        description=(
            "Busca en internet. Úsala para datos actuales o que no sabes con seguridad (resultados deportivos, "
            "precios, horarios, noticias concretas...). Devuelve títulos, fragmentos y enlaces; resume la respuesta "
            "y no leas las URLs en voz alta."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Qué buscar, en pocas palabras"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
        },
        fn=run,
        timeout_s=15,
    )


# --- Wikipedia --------------------------------------------------------------------------


class Wikipedia:
    def __init__(self, lang: str = "es", client: httpx.Client | None = None):
        self.base = f"https://{lang}.wikipedia.org"
        self._client = client or httpx.Client(timeout=8, headers={"User-Agent": UA}, follow_redirects=True)

    def summary(self, query: str) -> dict:
        try:
            found = self._client.get(f"{self.base}/w/rest.php/v1/search/page", params={"q": query, "limit": 1})
            found.raise_for_status()
            pages = found.json().get("pages") or []
            if not pages:
                raise ToolError(f"no hay ningún artículo sobre '{query}'")
            page = self._client.get(f"{self.base}/api/rest_v1/page/summary/{quote(pages[0]['key'], safe='')}")
            page.raise_for_status()
        except httpx.HTTPError as exc:
            raise ToolError(f"Wikipedia no responde ({type(exc).__name__})") from exc
        data = page.json()
        return {
            "title": data.get("title", pages[0].get("title", "")),
            "extract": _clean(data.get("extract", ""), 1200),
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
            "ambiguous": data.get("type") == "disambiguation",
        }


def wikipedia_tool(wiki: Wikipedia) -> Tool:
    def run(_ctx: ToolContext, topic: str) -> str:
        s = wiki.summary(topic)
        note = " (página de desambiguación: pide al usuario que concrete)" if s["ambiguous"] else ""
        return f"{s['title']}{note}: {s['extract']}"

    return Tool(
        name="wikipedia",
        description="Resumen de Wikipedia en español sobre una persona, lugar, concepto o hecho histórico.",
        parameters={
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "Tema a buscar"}},
            "required": ["topic"],
        },
        fn=run,
    )


# --- noticias -------------------------------------------------------------------------------


class News:
    FEED = "https://news.google.com/rss"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=8, headers={"User-Agent": UA}, follow_redirects=True)

    def headlines(self, topic: str = "", limit: int = 6) -> list[dict]:
        params = {"hl": "es", "gl": "ES", "ceid": "ES:es"}
        url = self.FEED
        if topic:
            url += "/search"
            params["q"] = f"{topic} when:2d"  # solo lo reciente
        try:
            resp = self._client.get(url, params=params)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except httpx.HTTPError as exc:
            raise ToolError(f"el servicio de noticias no responde ({type(exc).__name__})") from exc
        except ET.ParseError as exc:
            raise ToolError("respuesta de noticias no válida") from exc
        items = []
        for item in root.iter("item"):
            title = item.findtext("title", "")
            source = item.findtext("source", "")
            if source and title.endswith(f" - {source}"):
                title = title[: -len(source) - 3]
            try:
                when = parsedate_to_datetime(item.findtext("pubDate", "")).strftime("%d/%m %H:%M")
            except (TypeError, ValueError):
                when = ""
            items.append({"title": _clean(title, 160), "source": source, "when": when})
            if len(items) >= limit:
                break
        return items


def news_tool(news: News) -> Tool:
    def run(_ctx: ToolContext, topic: str = "", max_results: int = 6) -> str:
        items = news.headlines(topic, max_results)
        if not items:
            return "No hay noticias" + (f" sobre '{topic}'." if topic else ".")
        return "\n".join(f"- {n['title']} ({n['source']}, {n['when']})" for n in items)

    return Tool(
        name="news",
        description="Titulares de actualidad en España (Google Noticias). Sin tema: portada del día.",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Tema opcional (p. ej. 'Barça', 'economía', 'IA')"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
            },
        },
        fn=run,
    )


# --- unidades y divisas ------------------------------------------------------------------------

# factor a la unidad base de cada magnitud
UNITS: dict[str, tuple[str, float]] = {}


def _units(kind: str, base_factors: dict[str, float], aliases: dict[str, str]) -> None:
    for name, factor in base_factors.items():
        UNITS[name] = (kind, factor)
    for alias, name in aliases.items():
        UNITS[alias] = UNITS[name]


_units(
    "longitud",
    {"m": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048, "in": 0.0254, "nmi": 1852},
    {"metro": "m", "metros": "m", "kilometro": "km", "kilometros": "km", "centimetro": "cm", "centimetros": "cm",
     "milimetro": "mm", "milimetros": "mm", "milla": "mi", "millas": "mi", "yarda": "yd", "yardas": "yd",
     "pie": "ft", "pies": "ft", "pulgada": "in", "pulgadas": "in", "milla nautica": "nmi", "millas nauticas": "nmi"},
)
_units(
    "masa",
    {"kg": 1, "g": 0.001, "mg": 1e-6, "t": 1000, "lb": 0.45359237, "oz": 0.028349523125},
    {"kilo": "kg", "kilos": "kg", "kilogramo": "kg", "kilogramos": "kg", "gramo": "g", "gramos": "g",
     "miligramo": "mg", "miligramos": "mg", "tonelada": "t", "toneladas": "t", "libra": "lb", "libras": "lb",
     "onza": "oz", "onzas": "oz"},
)
_units(
    "volumen",
    {"l": 1, "ml": 0.001, "cl": 0.01, "m3": 1000, "gal": 3.785411784, "cup": 0.2365882365, "floz": 0.0295735295625},
    {"litro": "l", "litros": "l", "mililitro": "ml", "mililitros": "ml", "centilitro": "cl", "centilitros": "cl",
     "metro cubico": "m3", "metros cubicos": "m3", "galon": "gal", "galones": "gal", "taza": "cup", "tazas": "cup",
     "onza liquida": "floz", "onzas liquidas": "floz"},
)
_units(
    "velocidad",
    {"m/s": 1, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 0.514444},
    {"kmh": "km/h", "kilometros por hora": "km/h", "millas por hora": "mph", "nudo": "kn", "nudos": "kn",
     "metros por segundo": "m/s"},
)
_units(
    "superficie",
    {"m2": 1, "km2": 1e6, "ha": 1e4, "ft2": 0.09290304, "acre": 4046.8564224},
    {"metro cuadrado": "m2", "metros cuadrados": "m2", "kilometro cuadrado": "km2", "kilometros cuadrados": "km2",
     "hectarea": "ha", "hectareas": "ha", "pie cuadrado": "ft2", "pies cuadrados": "ft2", "acres": "acre"},
)
_units(
    "datos",
    {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4},
    {"byte": "b", "bytes": "b", "kilobyte": "kb", "kilobytes": "kb", "megabyte": "mb", "megabytes": "mb",
     "gigabyte": "gb", "gigabytes": "gb", "gigas": "gb", "terabyte": "tb", "terabytes": "tb", "teras": "tb"},
)
_units(
    "tiempo",
    {"s": 1, "min": 60, "h": 3600, "d": 86400, "sem": 604800},
    {"segundo": "s", "segundos": "s", "minuto": "min", "minutos": "min", "hora": "h", "horas": "h",
     "dia": "d", "dias": "d", "semana": "sem", "semanas": "sem"},
)
TEMPS = {"c": "c", "celsius": "c", "grados": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}


def _key(unit: str) -> str:
    import unicodedata

    plain = "".join(c for c in unicodedata.normalize("NFKD", unit) if not unicodedata.combining(c))
    return " ".join(plain.lower().replace("º", "").replace("°", "").split())


def convert_units(value: float, from_unit: str, to_unit: str) -> float:
    a, b = _key(from_unit), _key(to_unit)
    if a in TEMPS and b in TEMPS:
        c = {"c": lambda v: v, "f": lambda v: (v - 32) * 5 / 9, "k": lambda v: v - 273.15}[TEMPS[a]](value)
        return {"c": lambda v: v, "f": lambda v: v * 9 / 5 + 32, "k": lambda v: v + 273.15}[TEMPS[b]](c)
    if a not in UNITS or b not in UNITS:
        missing = from_unit if a not in UNITS else to_unit
        raise ToolError(f"no conozco la unidad '{missing}'")
    (ka, fa), (kb, fb) = UNITS[a], UNITS[b]
    if ka != kb:
        raise ToolError(f"no se puede convertir {ka} en {kb}")
    return value * fa / fb


def _fmt(x: float) -> str:
    if x != 0 and (abs(x) >= 1e9 or abs(x) < 1e-3):
        return f"{x:.4g}"
    return f"{x:,.4f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", ".")


class Currency:
    """Cambios oficiales del Banco Central Europeo (frankfurter.dev), actualizados cada día laborable."""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=8, headers={"User-Agent": UA})

    def convert(self, amount: float, src: str, dst: str) -> tuple[float, str]:
        src, dst = src.upper().strip(), dst.upper().strip()
        if not (re.fullmatch(r"[A-Z]{3}", src) and re.fullmatch(r"[A-Z]{3}", dst)):
            raise ToolError("usa códigos de divisa de 3 letras (EUR, USD, GBP...)")
        if src == dst:
            return amount, ""
        try:
            resp = self._client.get("https://api.frankfurter.dev/v1/latest", params={"base": src, "symbols": dst})
        except httpx.HTTPError as exc:
            raise ToolError(f"el servicio de cambio no responde ({type(exc).__name__})") from exc
        if resp.status_code == 404 or resp.status_code == 422:
            raise ToolError(f"divisa no soportada ({src} o {dst})")
        resp.raise_for_status()
        data = resp.json()
        return amount * data["rates"][dst], data.get("date", "")


def convert_tool(currency: Currency) -> Tool:
    def run(_ctx: ToolContext, value: float, from_unit: str, to_unit: str) -> str:
        if re.fullmatch(r"[A-Za-z]{3}", from_unit.strip()) and re.fullmatch(r"[A-Za-z]{3}", to_unit.strip()) and (
            _key(from_unit) not in UNITS and _key(to_unit) not in UNITS
        ):
            result, date = currency.convert(value, from_unit, to_unit)
            when = f" (cambio del BCE del {date})" if date else ""
            return f"{_fmt(value)} {from_unit.upper()} = {_fmt(round(result, 2))} {to_unit.upper()}{when}"
        return f"{_fmt(value)} {from_unit} = {_fmt(convert_units(value, from_unit, to_unit))} {to_unit}"

    return Tool(
        name="convert",
        description=(
            "Convierte unidades (longitud, peso, volumen, temperatura, velocidad, superficie, datos, tiempo) "
            "y divisas con el cambio del día (códigos de 3 letras: EUR, USD, GBP, JPY...)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "number"},
                "from_unit": {"type": "string", "description": "p. ej. 'km', 'libras', 'F', 'USD'"},
                "to_unit": {"type": "string", "description": "p. ej. 'millas', 'kg', 'C', 'EUR'"},
            },
            "required": ["value", "from_unit", "to_unit"],
        },
        fn=run,
    )


def info_tools(brave_key: str = "") -> list[Tool]:
    return [
        web_search_tool(WebSearch(brave_key)),
        wikipedia_tool(Wikipedia()),
        news_tool(News()),
        convert_tool(Currency()),
    ]
