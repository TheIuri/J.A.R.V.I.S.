"""Los informes, maquetados como documento y pasados a PDF aqui en el servidor.

El navegador imprimiendo una pagina web da un PDF pobre y distinto en cada movil. Aqui se monta el
documento entero (portada, cifras clave, graficas, tablas) y WeasyPrint lo convierte en un PDF de
verdad, con numeracion de paginas y la misma letra que el HUD. Se descarga como fichero: sin dialogo
de imprimir.

Nada de esto gasta tokens: el informe ya viene escrito y aqui solo se maqueta. Las cifras clave y las
graficas salen de leer la tabla del propio informe y el historico de precios.

Las graficas siguen las reglas de la guia de visualizacion: una sola serie por grafica, color que
destaca lo que importa y gris para el resto, etiquetas directas en vez de rejillas, y nunca el color
como unico dato (siempre hay etiqueta, y la tabla completa esta al lado).
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .prices import euros, from_report, parse_price, slug

log = logging.getLogger("jarvis.docgen")

FONTS = Path(__file__).with_name("web") / "fonts"
MAX_BARS = 8
MIN_POINTS = 3  # puntos minimos para dibujar la evolucion de un precio

# Paleta validada para fondo blanco (ver guia de visualizacion): un azul que destaca, gris para lo
# que acompana, y los colores de estado, que nunca van solos sin su etiqueta.
INK = "#0b0b0b"
INK_SOFT = "#52514e"
ACCENT = "#2a78d6"
ACCENT_SOFT = "#eaf1fc"
MUTED = "#c9ccd2"
GOOD = "#0ca30c"
BAD = "#d03b3b"
LINE = "#e6e8ec"


def available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:
        return False
    return True


# --- markdown -> html ---------------------------------------------------------

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])")
_CODE = re.compile(r"`([^`]+)`")
_BARE = re.compile(r"(?<![\"'>=])\bhttps?://[^\s<>)\]]+")


def inline(text: str) -> str:
    """Negritas, cursivas, codigo y enlaces. Todo escapado: el texto es del modelo, no HTML."""
    out = html.escape(text, quote=False)
    out = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>', out)
    out = _BARE.sub(lambda m: f'<a href="{html.escape(m.group(0), quote=True)}">{_short_url(m.group(0))}</a>', out)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return out


def _short_url(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").rstrip("/")


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _numeric(column: list[str]) -> bool:
    return bool(column) and all(re.fullmatch(r"[^A-Za-z]*\d[\d.,\s]*(?:[€$£%]|kg|g|mm|cm|h)?[^A-Za-z]*", c or "")
                                for c in column)


def markdown_html(text: str) -> tuple[str, str]:
    """(titulo del informe, html del cuerpo). El primer '# ' es el titulo y no se repite dentro."""
    lines = str(text or "").replace("\r", "").split("\n")
    title, out, i = "", [], 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue
        # Tabla: cabecera, separador y filas.
        if line.startswith("|") and re.match(r"^\|?\s*:?-{2,}", (lines[i + 1] if i + 1 < len(lines) else "").strip()):
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i]))
                i += 1
            right = [_numeric([r[c] for r in rows if c < len(r)]) for c in range(len(head))]
            ths = "".join(f'<th class="{"num" if right[c] else ""}">{inline(h)}</th>' for c, h in enumerate(head))
            trs = []
            for row in rows:
                tds = "".join(f'<td class="{"num" if c < len(right) and right[c] else ""}">{inline(v)}</td>'
                              for c, v in enumerate(row))
                trs.append(f"<tr>{tds}</tr>")
            out.append(f"<table><thead><tr>{ths}</tr></thead><tbody>{''.join(trs)}</tbody></table>")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            level, body = len(heading.group(1)), heading.group(2)
            if level == 1 and not title:
                title = re.sub(r"[*`]", "", body).strip()
            else:  # el "#" es el titulo del documento, asi que "##" es ya un apartado (h2)
                tag = f"h{min(4, level)}"
                out.append(f"<{tag}>{inline(body)}</{tag}>")
            i += 1
            continue
        item = re.match(r"^([-*•]|\d+[.)])\s+(.*)$", line)
        if item:
            tag = "ol" if re.match(r"\d", item.group(1)) else "ul"
            items = []
            while i < len(lines):
                m = re.match(r"^([-*•]|\d+[.)])\s+(.*)$", lines[i].strip())
                if not m or (tag == "ol") != bool(re.match(r"\d", m.group(1))):
                    break
                items.append(f"<li>{inline(m.group(2))}</li>")
                i += 1
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        # Parrafo: las lineas seguidas van juntas (el modelo corta los renglones).
        quote = line.startswith(">")
        parts = [line.lstrip("> ").strip()]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("|") or re.match(r"^#{1,4}\s", nxt)
                    or re.match(r"^([-*•]|\d+[.)])\s", nxt) or nxt.startswith(">") != quote):
                break
            parts.append(nxt.lstrip("> ").strip())
            i += 1
        tag = "blockquote" if quote else "p"
        out.append(f"<{tag}>{inline(' '.join(parts))}</{tag}>")
    return title, "\n".join(out)


# --- cifras clave y graficas ---------------------------------------------------

@dataclass(frozen=True)
class Figure:
    value: str
    label: str
    note: str = ""
    tone: str = ""  # "good" | "bad" | ""


def key_figures(report: str, cards: list[dict] | None = None, history: list[dict] | None = None) -> list[Figure]:
    """Lo que el usuario busca de un vistazo, sacado del propio informe (sin modelo)."""
    items = from_report(report, cards)
    figures: list[Figure] = []
    if items:
        cheap = min(items, key=lambda i: i["cents"])
        figures.append(Figure(euros(cheap["cents"]), "Más barato", cheap["name"][:38]))
        if len(items) > 1:
            dear = max(items, key=lambda i: i["cents"])
            figures.append(Figure(str(len(items)), "Opciones comparadas",
                                  f"de {euros(cheap['cents'])} a {euros(dear['cents'])}"))
    if history and len(history) >= 2:
        now, before = history[-1]["cents"], history[0]["cents"]
        if now != before and before:
            pct = round(100 * (now - before) / before)  # bajar es negativo, y para quien compra es bueno
            figures.append(Figure(f"{pct:+d}%".replace("-", "−"), "Desde que se vigila",
                                  f"antes {euros(before)}", "good" if now < before else "bad"))
    sources = len(set(re.findall(r"https?://[^\s<>)\]]+", report or "")))
    if sources:
        figures.append(Figure(str(sources), "Fuentes citadas"))
    checked = re.search(r"Comprobados (\d+) datos", report or "")
    if checked and len(figures) < 4:
        figures.append(Figure(checked.group(1), "Datos verificados"))
    return figures[:4]


def bars_html(report: str, cards: list[dict] | None = None) -> str:
    """Comparativa de precios: barras con el mas barato destacado y el valor en la punta.

    Una sola serie, asi que no hace falta leyenda; el color solo resalta el minimo y cada barra
    lleva su cifra al lado, que es lo que se lee.
    """
    items = from_report(report, cards)
    if len(items) < 2:
        return ""
    items = sorted(items, key=lambda i: i["cents"])[:MAX_BARS]
    top = max(i["cents"] for i in items)
    best = items[0]["cents"]
    rows = []
    for item in items:
        width = max(2.0, 100.0 * item["cents"] / top)
        fill = ACCENT if item["cents"] == best else MUTED
        rows.append(
            f'<div class="bar-row"><span class="bar-name">{html.escape(item["name"][:34])}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{width:.1f}%;background:{fill}"></span></span>'
            f'<span class="bar-value">{euros(item["cents"])}</span></div>'
        )
    return ('<figure class="chart"><figcaption>Precio por opción, de menor a mayor</figcaption>'
            + "".join(rows) + "</figure>")


def line_svg(history: list[dict], product: str) -> str:
    """Evolucion de un precio: una linea, su relleno suave y las cifras de los extremos."""
    points = [p for p in history if isinstance(p.get("cents"), int)][-30:]
    if len(points) < MIN_POINTS:
        return ""
    w, h = 720.0, 190.0
    pad_l, pad_r, pad_t, pad_b = 8.0, 74.0, 22.0, 26.0
    values = [p["cents"] for p in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or max(1, hi // 10)
    lo, hi = lo - span * 0.25, hi + span * 0.25
    def x(i: int) -> float:
        return pad_l + i * (w - pad_l - pad_r) / max(1, len(points) - 1)
    def y(v: int) -> float:
        return pad_t + (hi - v) * (h - pad_t - pad_b) / (hi - lo)
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
    area = f"{x(0):.1f},{h - pad_b:.1f} {line} {x(len(values) - 1):.1f},{h - pad_b:.1f}"
    first, last = points[0], points[-1]
    drop = last["cents"] < first["cents"]
    parts = [
        f'<svg viewBox="0 0 {w:.0f} {h:.0f}" width="100%" height="{h:.0f}">',
        f'<line x1="{pad_l}" y1="{h - pad_b:.1f}" x2="{w - pad_r:.1f}" y2="{h - pad_b:.1f}" '
        f'stroke="{LINE}" stroke-width="1"/>',
        f'<polygon points="{area}" fill="{ACCENT}" fill-opacity="0.1"/>',
        f'<polyline points="{line}" fill="none" stroke="{ACCENT}" stroke-width="2" '
        'stroke-linejoin="round" stroke-linecap="round"/>',
        f'<circle cx="{x(len(values) - 1):.1f}" cy="{y(values[-1]):.1f}" r="4" fill="{ACCENT}" '
        'stroke="#ffffff" stroke-width="2"/>',
        f'<text x="{x(len(values) - 1) + 10:.1f}" y="{y(values[-1]) + 4:.1f}" font-size="13" '
        f'font-weight="600" fill="{INK}">{euros(last["cents"])}</text>',
        f'<text x="{x(0):.1f}" y="{y(values[0]) - 10:.1f}" font-size="11" fill="{INK_SOFT}">'
        f'{euros(first["cents"])}</text>',
        f'<text x="{pad_l}" y="{h - 8:.1f}" font-size="10" fill="{INK_SOFT}">{html.escape(first["date"])}</text>',
        f'<text x="{w - pad_r:.1f}" y="{h - 8:.1f}" font-size="10" fill="{INK_SOFT}" text-anchor="end">'
        f'{html.escape(last["date"])}</text>',
        "</svg>",
    ]
    caption = (f'Evolución del precio de {html.escape(product[:40])}'
               f' · {"ha bajado" if drop else "ha subido"} desde {euros(first["cents"])}')
    return f'<figure class="chart"><figcaption>{caption}</figcaption>{"".join(parts)}</figure>'


# --- documento -----------------------------------------------------------------

def css(accent: str = ACCENT) -> str:
    inter = (FONTS / "inter-latin-variable.woff2").as_uri()
    return f"""
@font-face {{ font-family: Inter; src: url("{inter}") format("woff2");
  font-weight: 100 900; font-style: normal; }}
@page {{ size: A4; margin: 17mm 15mm 18mm;
  @bottom-left {{ content: string(doctitle); font: 7.5pt Inter; color: {INK_SOFT}; }}
  @bottom-right {{ content: counter(page) " / " counter(pages); font: 7.5pt Inter; color: {INK_SOFT}; }} }}
@page :first {{ @bottom-left {{ content: ""; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: {INK}; font: 10.5pt/1.55 Inter, sans-serif; }}
a {{ color: {INK}; text-decoration: none; border-bottom: .5pt solid {MUTED}; }}
.kicker {{ font-size: 7.5pt; font-weight: 700; letter-spacing: .16em; text-transform: uppercase; color: {accent}; }}
h1 {{ string-set: doctitle content(); margin: 3mm 0 0; font-size: 23pt; font-weight: 700; line-height: 1.12;
  letter-spacing: -0.028em; }}
.meta {{ margin: 3mm 0 0; font-size: 9pt; color: {INK_SOFT}; }}
.rule {{ height: 2.5pt; margin: 5mm 0 7mm; background: {accent}; }}
h2, h3, h4 {{ break-after: avoid; break-inside: avoid; letter-spacing: -0.012em; }}
h2 {{ margin: 8mm 0 2.5mm; font-size: 13.5pt; font-weight: 650; }}
h3 {{ margin: 6mm 0 2mm; font-size: 11.5pt; font-weight: 640; color: {INK}; }}
h4 {{ margin: 5mm 0 1.5mm; font-size: 10.5pt; font-weight: 640; }}
p {{ margin: 0 0 2.6mm; orphans: 3; widows: 3; }}
ul, ol {{ margin: 0 0 3mm; padding-left: 5mm; }}
li {{ margin: 0 0 1.2mm; break-inside: avoid; }}
blockquote {{ margin: 3.5mm 0; padding: 2.5mm 0 2.5mm 4mm; border-left: 2pt solid {accent};
  color: {INK_SOFT}; font-style: italic; }}
code {{ padding: .3mm 1mm; background: #f2f3f5; font-family: monospace; font-size: 9pt; }}

/* Cifras clave: lo que se busca de un vistazo. */
.figures {{ display: flex; gap: 3mm; margin: 0 0 7mm; }}
.fig {{ flex: 1; padding: 3.5mm 3.5mm 3mm; border-radius: 2mm; background: {ACCENT_SOFT};
  border-left: 1.5pt solid {accent}; break-inside: avoid; }}
.fig-value {{ font-size: 17pt; font-weight: 700; letter-spacing: -0.02em; line-height: 1.1; }}
.fig-value.good {{ color: {GOOD}; }}
.fig-value.bad {{ color: {BAD}; }}
.fig-label {{ margin-top: 1mm; font-size: 7.5pt; font-weight: 650; letter-spacing: .07em;
  text-transform: uppercase; color: {INK_SOFT}; }}
.fig-note {{ margin-top: .8mm; font-size: 8pt; color: {INK_SOFT}; }}

/* Graficas: una serie, etiquetas directas y nada de adornos. */
.chart {{ margin: 4mm 0 6mm; padding: 0; break-inside: avoid; }}
.chart figcaption {{ margin-bottom: 2.5mm; font-size: 8pt; font-weight: 650; letter-spacing: .06em;
  text-transform: uppercase; color: {INK_SOFT}; }}
.bar-row {{ display: flex; align-items: center; gap: 3mm; margin-bottom: 1.6mm; font-size: 9pt; }}
.bar-name {{ width: 42mm; overflow: hidden; white-space: nowrap; }}
.bar-track {{ flex: 1; }}
.bar-fill {{ display: block; height: 4.2mm; border-radius: 0 1.2mm 1.2mm 0; }}
.bar-value {{ width: 20mm; text-align: right; font-weight: 650; font-variant-numeric: tabular-nums; }}

/* Tablas: cabecera que se repite de pagina en pagina y cifras a la derecha. */
table {{ width: 100%; border-collapse: collapse; margin: 4mm 0 5mm; font-size: 9.5pt;
  border-top: 1.5pt solid {accent}; }}
thead {{ display: table-header-group; }}
th {{ padding: 2.2mm 2.5mm; border-bottom: .5pt solid {MUTED}; font-size: 8pt; font-weight: 700;
  letter-spacing: .06em; text-transform: uppercase; color: {INK_SOFT}; text-align: left; }}
td {{ padding: 2.2mm 2.5mm; border-bottom: .5pt solid {LINE}; vertical-align: top; }}
td:first-child {{ font-weight: 600; }}
tbody tr:nth-child(even) {{ background: #fafbfc; }}
tr {{ break-inside: avoid; }}
th.num, td.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
td a {{ border: 0; color: {INK_SOFT}; font-size: 8.5pt; }}
.source {{ margin-top: 9mm; padding-top: 2.5mm; border-top: .5pt solid {LINE}; font-size: 7.5pt;
  color: {INK_SOFT}; break-inside: avoid; }}
"""


def document_html(report: str, label: str = "", task: str = "", date: str = "", note: str = "",
                  cards: list[dict] | None = None, history: list[dict] | None = None,
                  product: str = "", accent: str = ACCENT) -> str:
    title, body = markdown_html(report)
    figures = key_figures(report, cards, history)
    head = [f'<div class="kicker">{html.escape(label or "Informe de JARVIS")}</div>',
            f'<h1>{html.escape(title or task or "Informe")}</h1>']
    meta = " · ".join(x for x in [long_date(date), task if title else ""] if x)
    if meta:
        head.append(f'<p class="meta">{html.escape(meta)}</p>')
    head.append('<div class="rule"></div>')
    if figures:
        tiles = "".join(
            f'<div class="fig"><div class="fig-value {f.tone}">{html.escape(f.value)}</div>'
            f'<div class="fig-label">{html.escape(f.label)}</div>'
            + (f'<div class="fig-note">{html.escape(f.note)}</div>' if f.note else "") + "</div>"
            for f in figures)
        head.append(f'<div class="figures">{tiles}</div>')
    charts = bars_html(report, cards) + line_svg(history or [], product or title or task)
    if charts and "<table>" in body:  # entran donde tienen sentido: justo antes de los datos
        body = body.replace("<table>", charts + "<table>", 1)
        charts = ""
    foot = (f'<div class="source">Informe de {html.escape(label or "un agente")} de JARVIS'
            + (f' · {html.escape(note)}' if note else "") + "</div>")
    return ("<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title or task or 'Informe')}</title>"
            f"<style>{css(accent)}</style></head><body>"
            + "".join(head) + charts + body + foot + "</body></html>")


def long_date(date: str) -> str:
    MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
             "octubre", "noviembre", "diciembre")
    try:
        when = datetime.fromisoformat(str(date).replace(" ", "T"))
    except ValueError:
        return str(date or "")
    return f"{when.day} de {MESES[when.month - 1]} de {when.year}"


def render_pdf(**kwargs: Any) -> bytes:
    """El PDF del informe. Lanza RuntimeError si la imagen no trae WeasyPrint."""
    try:
        from weasyprint import HTML
    except Exception as exc:  # imagen sin las librerias: el HUD imprime desde el navegador
        raise RuntimeError("WeasyPrint no está disponible en esta imagen") from exc
    return HTML(string=document_html(**kwargs), base_url=str(FONTS)).write_pdf()


def filename(task: str, date: str = "") -> str:
    name = re.sub(r"[^a-z0-9]+", "-", slug(task or "informe")).strip("-")[:60] or "informe"
    return f"{(date or '')[:10] or datetime.now():%Y-%m-%d}-{name}.pdf" if not date else f"{date[:10]}-{name}.pdf"


__all__ = ["available", "document_html", "render_pdf", "filename", "key_figures", "markdown_html",
           "bars_html", "line_svg", "from_report", "parse_price"]
