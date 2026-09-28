"""Configuracion leida de variables de entorno.

Todo proveedor es intercambiable cambiando solo variables, sin tocar codigo.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Esfuerzo de razonamiento por defecto: bajo, para que un asistente de voz responda rapido.
# Modelo de vision por proveedor (camara). Se cambia con <NOMBRE>_VISION_MODEL.
VISION_DEFAULTS = {
    "groq": "meta-llama/llama-4-scout-17b-16e-instruct",
    "gemini": "gemini-flash-latest",
    "mistral": "mistral-small-latest",  # Mistral Small tambien ve imagenes; Cerebras no tiene vision
    "openrouter": "meta-llama/llama-4-scout:free",
    "ollama": "llava",
}
REASONING_DEFAULTS = {"groq": "low", "cerebras": "low"}  # solo para modelos gpt-oss (los demas no razonan)

# Proveedores LLM con API compatible con OpenAI: (base_url, variable de la API key, modelo por defecto).
# Cualquiera se puede sobrescribir con <NOMBRE>_BASE_URL / <NOMBRE>_MODEL / <NOMBRE>_API_KEY,
# y <NOMBRE>_REASONING_EFFORT (low/medium/high) para modelos que razonan antes de responder.
# Los proveedores retiran modelos a menudo: si uno devuelve 404, mira su catalogo y cambia <NOMBRE>_MODEL.
LLM_PRESETS: dict[str, tuple[str, str | None, str]] = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        "gemini-flash-latest",  # alias de Google al Flash vigente: no se rompe cuando retiran uno
    ),
    # Capas gratuitas generosas (clave gratis en cloud.cerebras.ai y console.mistral.ai)
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b"),
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-latest"),
    "openrouter": (
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "meta-llama/llama-3.3-70b-instruct:free",
    ),
    "ollama": ("http://ollama:11434/v1", None, "qwen2.5:3b"),
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    return value in ("1", "true", "yes", "si", "on") if value else default


@dataclass(frozen=True)
class LLMProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str = ""


@dataclass(frozen=True)
class Settings:
    api_token: str
    assistant_name: str = "Jarvis"
    language: str = "es"
    data_dir: str = "/data"

    stt_provider: str = "local"  # local | groq
    whisper_model: str = "small"
    whisper_device: str = "auto"  # auto | cuda | cpu
    whisper_compute_type: str = "auto"
    groq_stt_model: str = "whisper-large-v3-turbo"

    llm_providers: list[LLMProviderConfig] = field(default_factory=list)
    agent_llm_providers: list[LLMProviderConfig] = field(default_factory=list)  # vacio = los mismos
    # Cadena propia por agente: AGENT_<NOMBRE>_PROVIDERS (p. ej. AGENT_COMPRAS_PROVIDERS="gemini,groq").
    agent_models: dict[str, list[LLMProviderConfig]] = field(default_factory=dict)
    vision_providers: list[LLMProviderConfig] = field(default_factory=list)  # camara; vacio = sin vision
    llm_timeout_s: int = 30
    llm_max_tokens: int = 1024  # incluye los tokens de razonamiento

    tts_provider: str = "piper"  # piper | edge (con Piper de respaldo) | none
    piper_voice: str = "es_ES-davefx-medium"
    piper_speaker: int | None = None  # voces con varios locutores (p. ej. sharvard: 0 hombre, 1 mujer)
    piper_speed: float = 1.0  # >1 mas rapido, <1 mas lento
    edge_voice: str = "es-ES-AlvaroNeural"  # TTS_PROVIDER=edge (Piper queda de respaldo)
    edge_rate: str = "+0%"
    edge_pitch: str = "+0Hz"

    history_turns: int = 6

    # Nivel 3: memoria
    memory_enabled: bool = True
    memory_max_items: int = 8  # recuerdos inyectados como maximo antes de cada respuesta

    # Obsidian: ruta de la boveda dentro del contenedor ("" = desactivado)
    obsidian_vault: str = ""
    obsidian_inbox: str = "Inbox"
    obsidian_daily: str = "Diario"
    obsidian_memory_note: bool = True  # JARVIS/Memoria.md con lo que recuerda

    # Nivel 2: tools
    tools_enabled: bool = True  # interruptor general
    tools_disabled: frozenset[str] = frozenset()
    timezone: str = "Europe/Madrid"
    home_city: str = ""
    home_coords: tuple[float, float] | None = None  # HOME_LATITUDE/HOME_LONGITUDE: evita buscar la ciudad
    truenas_url: str = ""  # p. ej. wss://192.168.1.10/api/current
    truenas_user: str = ""
    truenas_api_key: str = ""
    truenas_verify_ssl: bool = False  # TrueNAS usa un certificado autofirmado por defecto
    brave_api_key: str = ""  # opcional: busqueda web con Brave en vez de DuckDuckGo
    wol_devices: str = ""  # "sobremesa=AA:BB:CC:DD:EE:FF;otro=..."
    calendars: str = ""  # "personal=https://...ics;trabajo=https://...ics" (enlaces secretos iCal)
    # Crear eventos (client/calendar_login.py da los tokens)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"
    outlook_client_id: str = ""
    outlook_refresh_token: str = ""
    outlook_tenant: str = "common"
    ha_url: str = ""  # Home Assistant, p. ej. http://192.168.1.50:8123
    ha_token: str = ""
    ha_entities: str = ""  # opcional: prefijos de entity_id permitidos ("light.,switch.salon")
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_refresh_token: str = ""
    spotify_device: str = ""  # dispositivo preferido si no hay ninguno sonando
    notify_enabled: bool = True  # avisos proactivos (vigilantes + recordatorios)
    agents_enabled: bool = True  # Nivel 5: agente investigador en segundo plano
    notify_quiet: str = ""  # "23:00-08:00": avisos sin voz (salvo criticos)
    ntfy_url: str = ""  # push al movil: https://ntfy.sh/<tema-secreto> o tu servidor ntfy
    ntfy_token: str = ""
    calendar_remind_minutes: int = 15
    truenas_watch_minutes: int = 5
    disk_temp_warn: int = 50
    briefing_at: str = ""  # "08:00": resumen de buenos dias automatico
    briefing_weekends: bool = True
    summary_at: str = ""  # "23:30": resumen nocturno de lo hablado, en la nota del dia (necesita Obsidian)
    leads_profiles: dict[str, str] = field(default_factory=dict)  # LEADS_PROFILE_<NOMBRE>: varios productos
    leads_profile: str = ""  # lo que ofreces, para el captador de clientes (p. ej. "taller de impresion 3D en ...")
    insights_enabled: bool = True  # fichas con los datos clave de cada respuesta en el HUD
    wol_broadcast: str = "255.255.255.255"

    @property
    def groq_api_key(self) -> str:
        return _env("GROQ_API_KEY")


def _coords(lat: str, lon: str) -> tuple[float, float] | None:
    if not lat and not lon:
        return None
    try:
        return float(lat.replace(",", ".")), float(lon.replace(",", "."))
    except ValueError as exc:
        raise RuntimeError("HOME_LATITUDE y HOME_LONGITUDE deben ser numeros, p. ej. 41.508 y 2.117") from exc


def _llm_provider(spec: str) -> LLMProviderConfig:
    """"groq" (modelo de GROQ_MODEL o el de fabrica) o "groq:llama-3.3-70b-versatile" (ese modelo).
    Asi se encadenan varios modelos del mismo proveedor: en Groq cada modelo tiene su propio cupo."""
    name, _, explicit = spec.strip().partition(":")
    name, explicit = name.strip().lower(), explicit.strip()
    if name not in LLM_PRESETS:
        raise ValueError(f"Proveedor LLM desconocido: {name!r}. Opciones: {', '.join(LLM_PRESETS)}")
    base_url, key_var, model = LLM_PRESETS[name]
    prefix = name.upper()
    model = explicit or _env(f"{prefix}_MODEL", model)
    effort = REASONING_DEFAULTS.get(name, "") if "gpt-oss" in model else ""
    return LLMProviderConfig(
        name=name,
        base_url=_env(f"{prefix}_BASE_URL", base_url).rstrip("/"),
        api_key=_env(f"{prefix}_API_KEY") if key_var else "",
        model=model,
        # <NOMBRE>_REASONING_EFFORT vale para el modelo por defecto; uno explicito usa lo que le toca.
        reasoning_effort=effort if explicit else _env(f"{prefix}_REASONING_EFFORT", effort),
    )


def _vision_order(*chains: list[LLMProviderConfig]) -> list[LLMProviderConfig]:
    """VISION_PROVIDERS o, por defecto, los proveedores ya configurados (Gemini primero: ve mejor)."""
    seen: dict[str, LLMProviderConfig] = {}
    for chain in chains:
        for p in chain:
            seen.setdefault(p.name, p)
    names = [n.strip().lower() for n in _env("VISION_PROVIDERS").split(",") if n.strip()]
    if names:
        return [seen.get(n) or _llm_provider(n) for n in names]
    return sorted((p for p in seen.values() if p.name in VISION_DEFAULTS), key=lambda p: p.name != "gemini")


def _vision(p: LLMProviderConfig) -> LLMProviderConfig:
    model = _env(f"{p.name.upper()}_VISION_MODEL", VISION_DEFAULTS.get(p.name, p.model))
    # Los modelos de vision no admiten reasoning_effort.
    return LLMProviderConfig(name=p.name, base_url=p.base_url, api_key=p.api_key, model=model)


def load_settings() -> Settings:
    token = _env("API_TOKEN")
    if not token:
        raise RuntimeError("API_TOKEN es obligatorio: nunca expongas el servidor sin autenticacion.")

    names = [n.strip() for n in _env("LLM_PROVIDERS", "groq").split(",") if n.strip()]
    providers = [_llm_provider(n) for n in names]
    # El agente investigador lee paginas largas: puede ir con otra cadena (p. ej. "gemini,groq").
    agent_names = [n.strip() for n in _env("AGENT_LLM_PROVIDERS").split(",") if n.strip()]
    agent_providers = [_llm_provider(n) for n in agent_names] or providers
    agent_models = {}
    for var, value in os.environ.items():
        match = re.fullmatch(r"AGENT_([A-Z]+)_PROVIDERS", var)
        if match and match.group(1) != "LLM" and value.strip():
            agent_models[match.group(1).lower()] = [_llm_provider(n.strip()) for n in value.split(",") if n.strip()]
    every = providers + agent_providers + [p for chain in agent_models.values() for p in chain]
    missing = sorted({p.name for p in every if LLM_PRESETS[p.name][1] and not p.api_key})
    if missing:
        raise RuntimeError(f"Falta la API key de: {', '.join(missing)}")

    return Settings(
        api_token=token,
        assistant_name=_env("ASSISTANT_NAME", "Jarvis"),
        language=_env("LANGUAGE", "es"),
        data_dir=_env("DATA_DIR", "/data"),
        stt_provider=_env("STT_PROVIDER", "local").lower(),
        whisper_model=_env("WHISPER_MODEL", "small"),
        whisper_device=_env("WHISPER_DEVICE", "auto"),
        whisper_compute_type=_env("WHISPER_COMPUTE_TYPE", "auto"),
        groq_stt_model=_env("GROQ_STT_MODEL", "whisper-large-v3-turbo"),
        llm_providers=providers,
        agent_llm_providers=agent_providers,
        agent_models=agent_models,
        vision_providers=[_vision(p) for p in _vision_order(providers, agent_providers)],
        llm_timeout_s=_env_int("LLM_TIMEOUT_S", 30),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 1024),
        tts_provider=_env("TTS_PROVIDER", "piper").lower(),
        piper_voice=_env("PIPER_VOICE", "es_ES-davefx-medium"),
        piper_speaker=_env_int("PIPER_SPEAKER", -1) if _env("PIPER_SPEAKER") else None,
        piper_speed=_env_float("PIPER_SPEED", 1.0),
        edge_voice=_env("EDGE_VOICE", "es-ES-AlvaroNeural"),
        edge_rate=_env("EDGE_RATE", "+0%"),
        edge_pitch=_env("EDGE_PITCH", "+0Hz"),
        history_turns=_env_int("HISTORY_TURNS", 6),
        memory_enabled=_env_bool("MEMORY_ENABLED", True),
        memory_max_items=_env_int("MEMORY_MAX_ITEMS", 8),
        obsidian_vault=_env("OBSIDIAN_VAULT"),
        obsidian_inbox=_env("OBSIDIAN_INBOX", "Inbox"),
        obsidian_daily=_env("OBSIDIAN_DAILY_FOLDER", "Diario"),
        obsidian_memory_note=_env_bool("OBSIDIAN_MEMORY_NOTE", True),
        tools_enabled=_env_bool("TOOLS_ENABLED", True),
        tools_disabled=frozenset(n.strip() for n in _env("TOOLS_DISABLED").split(",") if n.strip()),
        timezone=_env("TZ", "Europe/Madrid"),
        home_city=_env("HOME_CITY"),
        home_coords=_coords(_env("HOME_LATITUDE"), _env("HOME_LONGITUDE")),
        truenas_url=_env("TRUENAS_URL"),
        truenas_user=_env("TRUENAS_USER"),
        truenas_api_key=_env("TRUENAS_API_KEY"),
        truenas_verify_ssl=_env_bool("TRUENAS_VERIFY_SSL", False),
        brave_api_key=_env("BRAVE_API_KEY"),
        wol_devices=_env("WOL_DEVICES"),
        calendars=_env("CALENDARS"),
        google_client_id=_env("GOOGLE_CLIENT_ID"),
        google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_env("GOOGLE_REFRESH_TOKEN"),
        google_calendar_id=_env("GOOGLE_CALENDAR_ID", "primary"),
        outlook_client_id=_env("OUTLOOK_CLIENT_ID"),
        outlook_refresh_token=_env("OUTLOOK_REFRESH_TOKEN"),
        outlook_tenant=_env("OUTLOOK_TENANT", "common"),
        ha_url=_env("HA_URL"),
        ha_token=_env("HA_TOKEN"),
        ha_entities=_env("HA_ENTITIES"),
        spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET"),
        spotify_refresh_token=_env("SPOTIFY_REFRESH_TOKEN"),
        spotify_device=_env("SPOTIFY_DEVICE"),
        notify_enabled=_env_bool("NOTIFY_ENABLED", True),
        agents_enabled=_env_bool("AGENTS_ENABLED", True),
        notify_quiet=_env("NOTIFY_QUIET"),
        ntfy_url=_env("NTFY_URL"),
        ntfy_token=_env("NTFY_TOKEN"),
        calendar_remind_minutes=_env_int("CALENDAR_REMIND_MINUTES", 15),
        truenas_watch_minutes=_env_int("TRUENAS_WATCH_MINUTES", 5),
        disk_temp_warn=_env_int("DISK_TEMP_WARN", 50),
        briefing_at=_env("BRIEFING_AT"),
        briefing_weekends=_env_bool("BRIEFING_WEEKENDS", True),
        summary_at=_env("SUMMARY_AT"),
        leads_profile=_env("LEADS_PROFILE"),
        leads_profiles={m.group(1).lower(): v.strip() for k, v in os.environ.items()
                        if (m := re.fullmatch(r"LEADS_PROFILE_([A-Z0-9]+)", k)) and v.strip()},
        insights_enabled=_env_bool("INSIGHTS_ENABLED", True),
        wol_broadcast=_env("WOL_BROADCAST", "255.255.255.255"),
    )
