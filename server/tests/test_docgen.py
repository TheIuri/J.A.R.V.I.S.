"""El informe maquetado como documento y pasado a PDF en el servidor."""

from __future__ import annotations

import pytest

from jarvis import docgen

REPORT = """# Catálogo de precios de Filamentor.io

Repaso de los precios por material.
Los precios llevan IVA.

## Precios por material

| Material | Precio mínimo | Marca | Enlace |
|---|---|---|---|
| PLA Basic 1 kg | 11,95 € | Filamentor | https://filamentor.es/pla-basic |
| PETG Blanco 1 kg | 6,50 € | Filamentor | https://filamentor.es/petg-blanco |
| Nylon PA12 1 kg | 46,00 € | Filamentor Pro | https://filamentor.es/nylon-pa12 |

## Recomendación

El **PETG blanco** es lo mejor si empiezas, según [su web](https://filamentor.es/envios).

- Descuento por volumen.
- Envío gratis desde 50 €.

> Los precios cambian cada semana.

## Verificación de datos

Comprobados 12 datos: todos aparecen en las fuentes leídas.
"""

HISTORY = [{"date": f"2026-09-{d:02d}", "cents": c} for d, c in
           [(1, 780), (8, 770), (16, 700), (24, 660), (28, 650)]]


# --- markdown ------------------------------------------------------------------

def test_markdown_saca_el_titulo_y_no_lo_repite():
    title, body = docgen.markdown_html(REPORT)
    assert title == "Catálogo de precios de Filamentor.io"
    assert "Catálogo de precios" not in body
    assert "<h2>Precios por material</h2>" in body


def test_markdown_junta_las_lineas_de_un_parrafo():
    _, body = docgen.markdown_html(REPORT)
    assert "<p>Repaso de los precios por material. Los precios llevan IVA.</p>" in body


def test_markdown_tabla_con_las_cifras_a_la_derecha():
    _, body = docgen.markdown_html(REPORT)
    assert '<th class="num">Precio mínimo</th>' in body
    assert '<td class="num">11,95 €</td>' in body
    assert '<td class="">PLA Basic 1 kg</td>' in body  # el texto, a la izquierda


def test_markdown_negritas_listas_citas_y_enlaces():
    _, body = docgen.markdown_html(REPORT)
    assert "<strong>PETG blanco</strong>" in body
    assert '<a href="https://filamentor.es/envios">su web</a>' in body
    assert "<ul><li>Descuento por volumen.</li>" in body
    assert "<blockquote>Los precios cambian cada semana.</blockquote>" in body


def test_markdown_escapa_el_html_del_modelo():
    _, body = docgen.markdown_html("# T\n\nUn <script>alert(1)</script> y a & b")
    assert "<script>" not in body and "&lt;script&gt;" in body and "a &amp; b" in body


# --- cifras clave ---------------------------------------------------------------

def test_cifras_clave():
    figures = {f.label: f for f in docgen.key_figures(REPORT, None, HISTORY)}
    assert figures["Más barato"].value == "6,50 €" and "PETG" in figures["Más barato"].note
    assert figures["Opciones comparadas"].value == "3"
    assert "Fuentes citadas" in figures  # 4 como mucho: las verificaciones se quedan fuera


def test_la_bajada_de_precio_es_negativa_y_buena():
    figure = next(f for f in docgen.key_figures(REPORT, None, HISTORY) if f.label == "Desde que se vigila")
    assert figure.value == "−17%" and figure.tone == "good"


def test_la_subida_de_precio_es_mala():
    subida = list(reversed(HISTORY))
    figure = next(f for f in docgen.key_figures(REPORT, None, subida) if f.label == "Desde que se vigila")
    assert figure.value.startswith("+") and figure.tone == "bad"


def test_sin_precios_no_inventa_cifras():
    figures = docgen.key_figures("# Nota\n\nUn texto sin datos.")
    assert figures == []


# --- gráficas --------------------------------------------------------------------

def test_barras_destacan_el_mas_barato():
    bars = docgen.bars_html(REPORT)
    assert bars.count("bar-row") == 3
    assert bars.index("PETG Blanco") < bars.index("PLA Basic") < bars.index("Nylon")  # de menor a mayor
    assert bars.count(docgen.ACCENT) == 1 and bars.count(docgen.MUTED) == 2  # solo el mínimo va en color


def test_sin_barras_con_una_sola_opcion():
    assert docgen.bars_html("| A | Precio |\n|---|---|\n| Uno | 5,00 € |") == ""


def test_la_linea_necesita_varios_puntos():
    assert docgen.line_svg(HISTORY[:2], "PETG") == ""
    svg = docgen.line_svg(HISTORY, "PETG")
    assert "<polyline" in svg and "6,50 €" in svg and "2026-09-01" in svg


# --- documento --------------------------------------------------------------------

def test_el_html_del_documento_lleva_todo():
    out = docgen.document_html(REPORT, label="Vigilante Filamentor", task="precios de filamento",
                               date="2026-09-30 14:20", note="JARVIS/x.md", history=HISTORY, product="PETG")
    assert "Vigilante Filamentor" in out and "30 de septiembre de 2026" in out
    doc = out.split("</style>", 1)[1]  # sin el CSS, que también nombra las clases
    assert 'class="bar-row"' in doc and "<polyline" in doc
    assert doc.index("bar-row") > doc.index("Repaso de los precios")  # las gráficas, tras la entrada
    assert doc.index("bar-row") < doc.index("<table>")  # y antes de los datos
    assert "JARVIS/x.md" in out


def test_nombre_del_fichero():
    assert docgen.filename("Muéstrame los precios de filamento", "2026-09-30") == \
        "2026-09-30-muestrame-precios-filamento.pdf"


@pytest.mark.skipif(not docgen.available(), reason="WeasyPrint no está instalado")
def test_pdf_de_verdad():
    pdf = docgen.render_pdf(report=REPORT, label="Vigilante Filamentor", task="precios", date="2026-09-30 14:20",
                            note="JARVIS/x.md", history=HISTORY, product="PETG Blanco 1 kg")
    assert pdf.startswith(b"%PDF") and len(pdf) > 5000


def test_sin_weasyprint_avisa(monkeypatch):
    import builtins

    real = builtins.__import__

    def falla(name, *args, **kw):
        if name == "weasyprint":
            raise ImportError("no está")
        return real(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", falla)
    with pytest.raises(RuntimeError, match="WeasyPrint"):
        docgen.render_pdf(report=REPORT)
