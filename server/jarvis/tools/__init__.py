"""Nivel 2: herramientas que JARVIS puede usar."""

from __future__ import annotations

import logging

from ..config import Settings
from ..memory import MemoryStore
from ..obsidian import Vault
from .basic import datetime_tool, weather_tool
from .info import info_tools
from .memory import memory_tools
from .obsidian import obsidian_tools
from .pc import pc_tools
from .registry import Tool, ToolContext, ToolError, ToolRegistry
from .truenas import truenas_tools

log = logging.getLogger(__name__)

__all__ = ["Tool", "ToolContext", "ToolError", "ToolRegistry", "build_registry"]


def build_registry(
    settings: Settings, memory: MemoryStore | None = None, vault: Vault | None = None
) -> ToolRegistry | None:
    if not settings.tools_enabled:
        log.info("Tools desactivadas (TOOLS_ENABLED=false)")
        return None
    registry = ToolRegistry(disabled=set(settings.tools_disabled))
    registry.register(datetime_tool(settings.timezone))
    registry.register(weather_tool(settings.home_city, home_coords=settings.home_coords))
    for tool in info_tools(settings.brave_api_key, settings.timezone):
        registry.register(tool)
    for tool in pc_tools():
        registry.register(tool)
    for tool in memory_tools(memory) if memory else []:
        registry.register(tool)
    for tool in obsidian_tools(vault) if vault else []:
        registry.register(tool)
    if settings.truenas_url and settings.truenas_api_key:
        if not settings.truenas_url.startswith("wss://"):
            # TrueNAS revoca las API keys usadas sin TLS.
            raise RuntimeError("TRUENAS_URL debe empezar por wss:// para no exponer la API key")
        for tool in truenas_tools(
            settings.truenas_url, settings.truenas_user, settings.truenas_api_key, settings.truenas_verify_ssl
        ):
            registry.register(tool)
    log.info("Tools activas: %s", ", ".join(registry.names()))
    return registry
