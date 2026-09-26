import json
import os
import re

import pytest

from jarvis.config import Settings
from jarvis.memory import MemoryStore
from jarvis.obsidian import MEMORY_NOTE, Vault, VaultError
from jarvis.tools import ToolContext, build_registry


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Proyectos").mkdir(parents=True)
    (root / ".obsidian").mkdir()
    (root / "Proyectos" / "Domótica.md").write_text("# Domótica\nInstalar Home Assistant en el NAS.\nComprar sensores Zigbee.\n")
    (root / "Compra.md").write_text("- leche\n- pan")  # sin salto de linea final
    (root / ".obsidian" / "secreto.md").write_text("config interna")
    return Vault(root, "Europe/Madrid")


def test_search_ranks_by_title_and_content_and_ignores_hidden(vault):
    hits = vault.search("¿qué apunté sobre domótica y sensores?")
    assert hits[0].path == "Proyectos/Domótica.md"
    assert "sensores" in hits[0].snippet.lower() or "domótica" in hits[0].snippet.lower()
    assert vault.search("config interna") == []
    assert vault.search("de la el") == []


def test_read_with_or_without_extension(vault):
    assert "Home Assistant" in vault.read("Proyectos/Domótica")
    assert "leche" in vault.read("Compra.md")
    with pytest.raises(VaultError, match="no existe"):
        vault.read("Nada")


@pytest.mark.parametrize("path", ["../fuera", "/etc/passwd", "Proyectos/../../fuera", ".obsidian/secreto"])
def test_paths_cannot_escape_or_reach_hidden(vault, path):
    with pytest.raises(VaultError):
        vault.read(path)


def test_symlink_escape_is_blocked(vault, tmp_path):
    (tmp_path / "fuera.md").write_text("privado")
    os.symlink(tmp_path / "fuera.md", vault.root / "enlace.md")
    with pytest.raises(VaultError, match="fuera"):
        vault.read("enlace.md")


def test_create_never_overwrites(vault):
    assert vault.create("Idea: robot", "Hacer un **robot**") == "Inbox/Idea robot.md"
    assert vault.create("Idea: robot", "Otra versión") == "Inbox/Idea robot (2).md"
    assert vault.read("Inbox/Idea robot") == "Hacer un **robot**\n"
    assert vault.create("Plan", "x", folder="Proyectos") == "Proyectos/Plan.md"
    with pytest.raises(VaultError):
        vault.create("x", "y", folder="../fuera")


def test_append_keeps_existing_content(vault):
    vault.append("Compra", "- huevos")
    assert vault.read("Compra") == "- leche\n- pan\n- huevos\n"
    with pytest.raises(VaultError):
        vault.append("NoExiste", "algo")


def test_daily_note(vault):
    rel = vault.append_daily("Llamar al fontanero")
    vault.append_daily("Comprar pilas")
    assert re.fullmatch(r"Diario/\d{4}-\d{2}-\d{2}\.md", rel)
    lines = vault.read(rel).splitlines()
    assert re.fullmatch(r"- \d{2}:\d{2} Llamar al fontanero", lines[0])
    assert lines[1].endswith("Comprar pilas")


def test_secrets_are_not_written(vault):
    with pytest.raises(VaultError, match="sensible"):
        vault.append_daily("La contraseña del router es hunter2")
    with pytest.raises(VaultError, match="vacío"):
        vault.create("x", "   ")


def test_memory_note_follows_the_store(vault, tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    store.on_change(lambda: vault.export_memory(store))
    store.add("Prefiere respuestas cortas", "preferencia")
    store.add("Su perro se llama Toby", "hecho")
    note = vault.read(MEMORY_NOTE)
    assert "## Preferencia\n- Prefiere respuestas cortas `#1`" in note
    assert "## Hecho\n- Su perro se llama Toby `#2`" in note
    store.delete(2)
    assert "Toby" not in vault.read(MEMORY_NOTE)
    assert not any(p.name.startswith(".jarvis-") for p in (vault.root / "JARVIS").iterdir())


def test_broken_listener_does_not_break_memory(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    store.on_change(lambda: 1 / 0)
    assert store.add("Algo", "hecho").id == 1


def test_obsidian_tools_via_registry(vault):
    reg = build_registry(Settings(api_token="t"), vault=vault)
    ctx = ToolContext()
    assert "Proyectos/Domótica.md" in reg.execute("obsidian_search", '{"query": "zigbee"}', ctx)
    assert "Home Assistant" in reg.execute("obsidian_read", '{"path": "Proyectos/Domótica.md"}', ctx)
    assert reg.execute("obsidian_create_note", json.dumps({"title": "Viaje", "content": "Ir a Roma"}), ctx) == "Nota creada: Inbox/Viaje.md"
    assert reg.execute("obsidian_append", '{"path": "Compra", "text": "- café"}', ctx) == "Añadido a Compra.md"
    assert reg.execute("obsidian_daily_note", '{"text": "Probar JARVIS"}', ctx).startswith("Apuntado en Diario/")
    assert reg.execute("obsidian_read", '{"path": "../../etc/passwd"}', ctx) == "ERROR: ruta fuera de la bóveda"
    assert "obsidian_search" not in build_registry(Settings(api_token="t")).names()


def test_missing_vault_is_a_clear_error(tmp_path):
    with pytest.raises(VaultError, match="no existe"):
        Vault(tmp_path / "no-existe")
