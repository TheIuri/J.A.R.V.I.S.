"""Nivel 2: herramientas que JARVIS puede usar."""

from __future__ import annotations

import logging

from ..config import Settings
from .basic import datetime_tool, weather_tool
from .pc import pc_tools
from .registry import Tool, ToolContext, ToolError, ToolRegistry
from .truenas import truenas_tool

log = logging.getLogger(__name__)

__all__ = ["Tool", "ToolContext", "ToolError", "ToolRegistry", "build_registry"]


def build_registry(settings: Settings) -> ToolRegistry | None:
    if not settings.tools_enabled:
        log.info("Tools desactivadas (TOOLS_ENABLED=false)")
        return None
    registry = ToolRegistry(disabled=set(settings.tools_disabled))
    registry.register(datetime_tool(settings.timezone))
    registry.register(weather_tool(settings.home_city))
    for tool in pc_tools():
        registry.register(tool)
    if settings.truenas_url and settings.truenas_api_key:
        if not settings.truenas_url.startswith("wss://"):
            # TrueNAS revoca las API keys usadas sin TLS.
            raise RuntimeError("TRUENAS_URL debe empezar por wss:// para no exponer la API key")
        registry.register(
            truenas_tool(
                settings.truenas_url, settings.truenas_user, settings.truenas_api_key, settings.truenas_verify_ssl
            )
        )
    log.info("Tools activas: %s", ", ".join(registry.names()))
    return registry
