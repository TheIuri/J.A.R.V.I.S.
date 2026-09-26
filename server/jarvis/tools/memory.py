"""Tools para que el LLM gestione la memoria (Nivel 3)."""

from __future__ import annotations

from ..memory import TYPES, MemoryRejected, MemoryStore, keywords
from .registry import Tool, ToolContext, ToolError


def memory_tools(store: MemoryStore) -> list[Tool]:
    def save(_ctx: ToolContext, content: str, type: str = "hecho") -> str:
        try:
            m = store.add(content, type, source="usuario")
        except MemoryRejected as exc:
            raise ToolError(str(exc)) from exc
        return f"Guardado como recuerdo {m.id}."

    def search(_ctx: ToolContext, query: str) -> str:
        found = store.search(keywords(query), limit=8)
        return "\n".join(m.line() for m in found) or "No hay recuerdos sobre eso."

    def update(_ctx: ToolContext, id: int, content: str) -> str:
        try:
            m = store.update(id, content)
        except MemoryRejected as exc:
            raise ToolError(str(exc)) from exc
        return f"Recuerdo {m.id} actualizado."

    def forget(_ctx: ToolContext, id: int) -> str:
        try:
            m = store.delete(id)
        except MemoryRejected as exc:
            raise ToolError(str(exc)) from exc
        return f"Olvidado: {m.content}"

    return [
        Tool(
            name="memory_save",
            description=(
                "Guarda algo que el usuario quiere que recuerdes o un dato duradero suyo que cambiará respuestas "
                "futuras (nombre, gustos, familia, proyectos, decisiones). No guardes charla trivial, "
                "datos que ya recuerdas ni secretos (contraseñas, claves, tarjetas)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Frase autocontenida, p. ej. 'Su hija se llama Ana'."},
                    "type": {"type": "string", "enum": list(TYPES)},
                },
                "required": ["content"],
            },
            fn=save,
        ),
        Tool(
            name="memory_search",
            description="Busca en la memoria recuerdos que no te han llegado en el contexto.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            fn=search,
        ),
        Tool(
            name="memory_update",
            description="Corrige un recuerdo existente (por su id) cuando el usuario da información nueva.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1}, "content": {"type": "string"}},
                "required": ["id", "content"],
            },
            fn=update,
        ),
        Tool(
            name="memory_forget",
            description="Borra un recuerdo (por su id) cuando el usuario pide olvidarlo. Si no sabes el id, búscalo antes.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 1}},
                "required": ["id"],
            },
            fn=forget,
        ),
    ]
