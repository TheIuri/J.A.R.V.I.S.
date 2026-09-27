"""Configuracion leida de variables de entorno.

Todo proveedor es intercambiable cambiando solo variables, sin tocar codigo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Esfuerzo de razonamiento por defecto: bajo, para que un asistente de voz responda rapido.
REASONING_DEFAULTS = {"groq": "low"}

# Proveedores LLM con API compatible con OpenAI: (base_url, variable de la API key, modelo por defecto).
# Cualquiera se puede sobrescribir con <NOMBRE>_BASE_URL / <NOMBRE>_MODEL / <NOMBRE>_API_KEY,
# y <NOMBRE>_REASONING_EFFORT (low/medium/high) para modelos que razonan antes de responder.
# Los proveedores retiran modelos a menudo: si uno devuelve 404, mira su catalogo y cambia <NOMBRE>_MODEL.
LLM_PRESETS: dict[str, tuple[str, str | None, str]] = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
        "gemini-2.5-flash",
    ),
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
    llm_timeout_s: int = 30
    llm_max_tokens: int = 1024  # incluye los tokens de razonamiento

    tts_provider: str = "piper"  # piper | none
    piper_voice: str = "es_ES-davefx-medium"
    piper_speaker: int | None = None  # voces con varios locutores (p. ej. sharvard: 0 hombre, 1 mujer)
    piper_speed: float = 1.0  # >1 mas rapido, <1 mas lento

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


def _llm_provider(name: str) -> LLMProviderConfig:
    if name not in LLM_PRESETS:
        raise ValueError(f"Proveedor LLM desconocido: {name!r}. Opciones: {', '.join(LLM_PRESETS)}")
    base_url, key_var, model = LLM_PRESETS[name]
    prefix = name.upper()
    return LLMProviderConfig(
        name=name,
        base_url=_env(f"{prefix}_BASE_URL", base_url).rstrip("/"),
        api_key=_env(f"{prefix}_API_KEY") if key_var else "",
        model=_env(f"{prefix}_MODEL", model),
        reasoning_effort=_env(f"{prefix}_REASONING_EFFORT", REASONING_DEFAULTS.get(name, "")),
    )


def load_settings() -> Settings:
    token = _env("API_TOKEN")
    if not token:
        raise RuntimeError("API_TOKEN es obligatorio: nunca expongas el servidor sin autenticacion.")

    names = [n.strip().lower() for n in _env("LLM_PROVIDERS", "groq").split(",") if n.strip()]
    providers = [_llm_provider(n) for n in names]
    missing = [p.name for p in providers if LLM_PRESETS[p.name][1] and not p.api_key]
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
        llm_timeout_s=_env_int("LLM_TIMEOUT_S", 30),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 1024),
        tts_provider=_env("TTS_PROVIDER", "piper").lower(),
        piper_voice=_env("PIPER_VOICE", "es_ES-davefx-medium"),
        piper_speaker=_env_int("PIPER_SPEAKER", -1) if _env("PIPER_SPEAKER") else None,
        piper_speed=_env_float("PIPER_SPEED", 1.0),
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
        wol_broadcast=_env("WOL_BROADCAST", "255.255.255.255"),
    )
