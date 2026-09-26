"""Tools que se ejecutan en el PC del usuario.

El servidor no toca el PC: valida la peticion y la devuelve en la respuesta como una accion.
El cliente la ejecuta solo si esta en su propia lista permitida (doble control).
"""

from __future__ import annotations

from urllib.parse import urlparse

from .registry import Tool, ToolContext, ToolError


def _queue(ctx: ToolContext, action: str, **args) -> str:
    ctx.pc_actions.append({"action": action, **args})
    return "Acción enviada al PC."


def _open_app(ctx: ToolContext, app: str) -> str:
    return _queue(ctx, "open_app", app=app)


def _open_url(ctx: ToolContext, url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError("solo se pueden abrir URLs http(s) completas")
    return _queue(ctx, "open_url", url=url)


def _volume(ctx: ToolContext, action: str, level: int | None = None) -> str:
    if action == "set" and level is None:
        raise ToolError("para 'set' hace falta 'level' (0-100)")
    return _queue(ctx, "volume", mode=action, level=level)


def _media(ctx: ToolContext, action: str) -> str:
    return _queue(ctx, "media", key=action)


def _timer(ctx: ToolContext, seconds: int, label: str = "") -> str:
    _queue(ctx, "timer", seconds=seconds, label=label)
    minutes, secs = divmod(seconds, 60)
    return f"Temporizador programado en el PC ({minutes} min {secs} s)."


def pc_tools() -> list[Tool]:
    return [
        Tool(
            name="pc_open_app",
            description="Abre una aplicación en el PC del usuario. Solo las de la lista permitida.",
            parameters={
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Nombre de la app de la lista."}},
                "required": ["app"],
            },
            fn=_open_app,
            pc=True,
        ),
        Tool(
            name="pc_open_url",
            description="Abre una página web en el navegador del PC.",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "URL completa con https://"}},
                "required": ["url"],
            },
            fn=_open_url,
            pc=True,
        ),
        Tool(
            name="pc_volume",
            description="Controla el volumen del PC: fijarlo (set, con level 0-100), subir, bajar o silenciar.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["set", "up", "down", "mute"]},
                    "level": {"type": "integer", "minimum": 0, "maximum": 100},
                },
                "required": ["action"],
            },
            fn=_volume,
            pc=True,
        ),
        Tool(
            name="pc_media",
            description="Controla la música/vídeo que suena en el PC.",
            parameters={
                "type": "object",
                "properties": {"action": {"type": "string", "enum": ["play_pause", "next", "previous"]}},
                "required": ["action"],
            },
            fn=_media,
            pc=True,
        ),
        Tool(
            name="pc_timer",
            description="Pone un temporizador o recordatorio que avisará por voz en el PC.",
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
                    "label": {"type": "string", "description": "Para qué es, p. ej. 'la pasta'."},
                },
                "required": ["seconds"],
            },
            fn=_timer,
            pc=True,
        ),
    ]
