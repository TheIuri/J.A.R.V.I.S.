"""Búsqueda por significado en las notas: encuentra sin la palabra exacta y no inventa."""

from __future__ import annotations

import pytest

from jarvis.notes_index import NotesIndex, split_note, stem, terms
from jarvis.obsidian import Vault
from jarvis.tools.obsidian import obsidian_tools
from jarvis.tools.registry import ToolContext

NOTAS = {
    "Proveedores/Filamentor.md": (
        "# Filamentor\nQuien me vende el PLA y el PETG para la impresora.\n"
        "Contacto: Marta, responde rápido.\n\n## Envíos\nGratis desde 50 euros.\n"
    ),
    "Proyectos/Impresora.md": "# Impresora 3D\nCambié la boquilla a 0,6 mm. Imprime mejor el PETG.\n",
    "Diario/2026-09-01.md": "- 10:00 Reunión con el gestor sobre los impuestos del trimestre\n",
    "Recetas/Lentejas.md": "# Lentejas\nChorizo, patata y zanahoria. A fuego lento 40 minutos.\n",
}


@pytest.fixture()
def index(tmp_path):
    for rel, text in NOTAS.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return NotesIndex(Vault(tmp_path))


def test_stem_quita_terminaciones():
    assert stem("filamentos") == stem("filamento")
    assert stem("lentejas") == stem("lenteja")
    assert stem("sol") == "sol"  # las palabras cortas se quedan como están


def test_terms_trae_palabras_y_trozos():
    t = terms("filamento PETG")
    assert "w:filament" in t and any(x.startswith("n:") for x in t)


def test_split_note_por_titulos():
    trozos = split_note(NOTAS["Proveedores/Filamentor.md"], "Filamentor")
    assert [t for t, _ in trozos] == ["Filamentor", "Envíos"]


def test_encuentra_sin_la_palabra_exacta(index):
    # "proveedor de filamento" no aparece en la nota: dice "quien me vende el PLA".
    found = index.search("¿quién es mi proveedor de filamento?")
    assert found and found[0].path == "Proveedores/Filamentor.md"


def test_no_devuelve_lo_que_no_tiene_que_ver(index):
    paths = [f.path for f in index.search("lentejas con chorizo")]
    assert paths and paths[0] == "Recetas/Lentejas.md"
    assert "Proveedores/Filamentor.md" not in paths


def test_una_entrada_por_nota_y_con_fragmento(index):
    found = index.search("PETG impresora")
    assert len(found) == len({f.path for f in found})
    assert all(f.snippet for f in found)
    assert "›" in found[0].line() or found[0].path in found[0].line()


def test_sin_resultados_si_no_hay_nada_parecido(index):
    assert index.search("cotización del bitcoin en yenes") == []


def test_el_indice_se_rehace_al_cambiar_una_nota(index, tmp_path):
    assert index.search("submarinismo") == []
    nota = tmp_path / "Ocio/Buceo.md"
    nota.parent.mkdir(parents=True, exist_ok=True)
    nota.write_text("# Buceo\nCurso de submarinismo en Denia, en junio.\n", encoding="utf-8")
    assert [f.path for f in index.search("submarinismo")] == ["Ocio/Buceo.md"]


def test_la_tool_usa_el_indice_y_cae_a_la_busqueda_literal(index):
    tool = {t.name: t for t in obsidian_tools(index.vault, index)}["obsidian_search"]
    assert "Filamentor" in tool.fn(ToolContext(), query="quién me vende el filamento")
    assert "No hay notas" in tool.fn(ToolContext(), query="zzzz")
