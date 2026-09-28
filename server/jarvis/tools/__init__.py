"""Nivel 2: herramientas que JARVIS puede usar."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from ..memory import MemoryStore
from ..obsidian import Vault
from .basic import datetime_tool, weather_tool
from .calendar import Calendars, calendar_tool, parse_calendars
from .calendar_write import GoogleCalendar, OutlookCalendar, calendar_add_tool
from .delegate import delegate_tool
from .homeassistant import HomeAssistant, ha_tools
from .info import info_tools
from .memory import memory_tools
from .obsidian import obsidian_tools
from .pc import pc_tools
from .registry import Tool, ToolContext, ToolError, ToolRegistry
from .reminders import ReminderStore, reminder_tools
from .spotify import Spotify, spotify_tools
from .security import audit_tool, security_snapshot
from .truenas import _default_connect, truenas_tools
from .vision import Vision, camera_tool
from .wol import parse_devices, wol_tool

log = logging.getLogger(__name__)

__all__ = ["Tool", "ToolContext", "ToolError", "ToolRegistry", "build_registry"]


def build_registry(
    settings: Settings,
    memory: MemoryStore | None = None,
    vault: Vault | None = None,
    reminders: ReminderStore | None = None,
    calendars: Calendars | None = None,
    vision: Vision | None = None,
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
    registry.register(delegate_tool())  # solo se ofrece con el HUD del PC
    registry.register(audit_tool(_snapshot(settings)))  # Claude en el PC; siempre con confirmacion
    if vision:
        registry.register(camera_tool(vision))  # solo se ofrece si el HUD manda foto
    if calendars is None and settings.calendars:
        calendars = Calendars(parse_calendars(settings.calendars), settings.timezone)
    if calendars:
        registry.register(calendar_tool(calendars))
    writers = _calendar_writers(settings)
    if writers:
        registry.register(calendar_add_tool(writers, settings.timezone))
    for tool in reminder_tools(reminders) if reminders else []:
        registry.register(tool)
    if settings.ha_url and settings.ha_token:
        for tool in ha_tools(HomeAssistant(settings.ha_url, settings.ha_token, settings.ha_entities), vision):
            registry.register(tool)
    if settings.spotify_client_id and settings.spotify_client_secret and settings.spotify_refresh_token:
        spotify = Spotify(
            settings.spotify_client_id,
            settings.spotify_client_secret,
            settings.spotify_refresh_token,
            settings.spotify_device,
        )
        for tool in spotify_tools(spotify):
            registry.register(tool)
    if settings.wol_devices:
        registry.register(wol_tool(parse_devices(settings.wol_devices), settings.wol_broadcast))
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


def _calendar_writers(settings: Settings) -> dict:
    """Calendarios donde JARVIS puede crear eventos (cada uno con su inicio de sesion)."""
    writers: dict = {}
    if settings.google_client_id and settings.google_client_secret and settings.google_refresh_token:
        writers["google"] = GoogleCalendar(
            settings.google_client_id, settings.google_client_secret, settings.google_refresh_token,
            settings.google_calendar_id,
        )
    if settings.outlook_client_id and settings.outlook_refresh_token:
        writers["outlook"] = OutlookCalendar(
            settings.outlook_client_id, settings.outlook_refresh_token, settings.outlook_tenant,
            Path(settings.data_dir) / "outlook_token.json",
        )
    return writers


def _snapshot(settings: Settings):
    """Foto de seguridad de TrueNAS para el auditor (None si TrueNAS no esta configurado)."""
    if not (settings.truenas_url and settings.truenas_api_key):
        return None

    def snapshot() -> str:
        client = _default_connect(
            settings.truenas_url, settings.truenas_user, settings.truenas_api_key, settings.truenas_verify_ssl
        )
        try:
            return security_snapshot(client.call)
        finally:
            client.close()

    return snapshot
