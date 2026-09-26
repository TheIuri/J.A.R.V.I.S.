"""Tools que corren en el servidor y no necesitan cuentas: fecha/hora y tiempo (Open-Meteo)."""

from __future__ import annotations

import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .registry import Tool, ToolContext, ToolError

DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Codigos WMO que usa Open-Meteo.
WEATHER_CODES = {
    0: "despejado", 1: "casi despejado", 2: "parcialmente nublado", 3: "nublado",
    45: "niebla", 48: "niebla con escarcha",
    51: "llovizna débil", 53: "llovizna", 55: "llovizna intensa",
    61: "lluvia débil", 63: "lluvia", 65: "lluvia intensa",
    66: "lluvia helada", 67: "lluvia helada intensa",
    71: "nieve débil", 73: "nieve", 75: "nieve intensa", 77: "granizo fino",
    80: "chubascos débiles", 81: "chubascos", 82: "chubascos fuertes",
    85: "chubascos de nieve", 86: "chubascos de nieve fuertes",
    95: "tormenta", 96: "tormenta con granizo", 99: "tormenta fuerte con granizo",
}


def format_datetime(now: datetime) -> str:
    return (
        f"{DAYS[now.weekday()]} {now.day} de {MONTHS[now.month - 1]} de {now.year}, "
        f"{now:%H:%M} ({now.tzname()})"
    )


def datetime_tool(timezone: str) -> Tool:
    tz = ZoneInfo(timezone)

    def run(_ctx: ToolContext) -> str:
        return format_datetime(datetime.now(tz))

    return Tool(
        name="get_datetime",
        description="Devuelve la fecha y hora actuales del usuario.",
        parameters={"type": "object", "properties": {}},
        fn=run,
    )


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower().strip()


def _score(result: dict, name: str, hint: str) -> int:
    """Nombre exacto (sin acentos) y coincidencia de provincia/pais ganan; empate = orden de la API."""
    score = 2 if _norm(result.get("name", "")) == _norm(name) else 0
    if hint:
        regions = [result.get(k, "") for k in ("admin1", "admin2", "admin3", "admin4", "country")]
        score += any(_norm(hint) == _norm(r) for r in regions if r)
    return score


class OpenMeteo:
    GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=8)

    def _search(self, name: str) -> list[dict]:
        resp = self._client.get(self.GEO_URL, params={"name": name, "count": 10, "language": "es"})
        resp.raise_for_status()
        return resp.json().get("results") or []

    def geocode(self, query: str) -> dict:
        """Busca una ciudad. Acepta "Ciudad, Provincia/País": el buscador solo entiende el nombre,
        asi que la parte tras la coma se usa para elegir entre candidatos."""
        name, _, hint = (part.strip() for part in query.partition(","))
        tries = [query]
        if hint:
            tries.append(name)
        if len(name.split()) > 1:
            tries.append(name.split()[0])  # "Badia del Valles" -> "Badia" (acentos, grafias)
        for q in dict.fromkeys(tries):
            results = self._search(q)
            if results:
                return max(results, key=lambda r: _score(r, name, hint))
        raise ToolError(f"no encuentro la ciudad '{query}'")

    def forecast(self, lat: float, lon: float, days: int) -> dict:
        resp = self._client.get(
            self.FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": days,
            },
        )
        resp.raise_for_status()
        return resp.json()


def weather_tool(
    default_city: str, api: OpenMeteo | None = None, home_coords: tuple[float, float] | None = None
) -> Tool:
    api = api or OpenMeteo()

    def run(_ctx: ToolContext, city: str = "", days: int = 1) -> str:
        city = city.strip() or default_city
        if not city and not home_coords:
            raise ToolError("no se ha indicado ciudad y no hay HOME_CITY configurada")
        try:
            if home_coords and _norm(city) == _norm(default_city):
                # Coordenadas fijas: no depende del buscador de ciudades.
                place = {"name": default_city or "casa", "latitude": home_coords[0], "longitude": home_coords[1]}
            else:
                place = api.geocode(city)
            data = api.forecast(place["latitude"], place["longitude"], days)
        except httpx.HTTPError as exc:
            raise ToolError(f"servicio del tiempo no disponible: {exc}") from exc

        cur = data["current"]
        where = f"{place['name']} ({place['country']})" if place.get("country") else place["name"]
        lines = [
            f"{where} ahora: "
            f"{WEATHER_CODES.get(cur['weather_code'], 'desconocido')}, {cur['temperature_2m']:.0f} °C "
            f"(sensación {cur['apparent_temperature']:.0f} °C), viento {cur['wind_speed_10m']:.0f} km/h."
        ]
        d = data["daily"]
        for i, day in enumerate(d["time"]):
            label = "Hoy" if i == 0 else ("Mañana" if i == 1 else day)
            rain = d["precipitation_probability_max"][i]
            lines.append(
                f"{label}: {WEATHER_CODES.get(d['weather_code'][i], 'desconocido')}, "
                f"{d['temperature_2m_min'][i]:.0f}–{d['temperature_2m_max'][i]:.0f} °C"
                + (f", {rain}% prob. de lluvia" if rain is not None else "")
                + "."
            )
        return "\n".join(lines)

    return Tool(
        name="get_weather",
        description=(
            "Tiempo actual y previsión. Si el usuario no dice ciudad, deja 'city' vacío "
            "y se usará su ciudad por defecto."
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Ciudad, p. ej. 'Madrid'. Vacío = ciudad del usuario."},
                "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Días de previsión (1 = hoy)."},
            },
        },
        fn=run,
    )
