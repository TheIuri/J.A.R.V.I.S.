"""Tools de Obsidian: buscar, leer, crear, añadir y apuntar en la nota del día."""

from __future__ import annotations

from ..obsidian import Vault, VaultError
from .registry import Tool, ToolContext, ToolError


def obsidian_tools(vault: Vault) -> list[Tool]:
    def guard(fn, *args):
        try:
            return fn(*args)
        except VaultError as exc:
            raise ToolError(str(exc)) from exc

    def search(_ctx: ToolContext, query: str) -> str:
        hits = guard(vault.search, query)
        return "\n".join(f"- {h.path}: {h.snippet}" for h in hits) or "No hay notas sobre eso."

    def read(_ctx: ToolContext, path: str) -> str:
        return guard(vault.read, path)

    def create(_ctx: ToolContext, title: str, content: str, folder: str = "") -> str:
        return f"Nota creada: {guard(vault.create, title, content, folder)}"

    def append(_ctx: ToolContext, path: str, text: str) -> str:
        return f"Añadido a {guard(vault.append, path, text)}"

    def daily(_ctx: ToolContext, text: str) -> str:
        return f"Apuntado en {guard(vault.append_daily, text)}"

    return [
        Tool(
            name="obsidian_search",
            description="Busca en las notas de Obsidian del usuario. Devuelve rutas y un fragmento de cada nota.",
            parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            fn=search,
        ),
        Tool(
            name="obsidian_read",
            description="Lee una nota de Obsidian por su ruta (como la devuelve obsidian_search).",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            fn=read,
        ),
        Tool(
            name="obsidian_create_note",
            description=(
                "Crea una nota nueva en Obsidian (por defecto en la bandeja de entrada). Nunca sobrescribe: "
                "si el título existe, crea otra con sufijo. Usa Markdown en el contenido."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "folder": {"type": "string", "description": "Carpeta opcional dentro de la bóveda."},
                },
                "required": ["title", "content"],
            },
            fn=create,
        ),
        Tool(
            name="obsidian_append",
            description="Añade texto al final de una nota existente (p. ej. la lista de la compra). Búscala antes si no sabes la ruta.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                "required": ["path", "text"],
            },
            fn=append,
        ),
        Tool(
            name="obsidian_daily_note",
            description="Apunta algo en la nota de hoy (diario), con la hora. Para 'apunta que...' sin nota concreta.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            fn=daily,
        ),
    ]
