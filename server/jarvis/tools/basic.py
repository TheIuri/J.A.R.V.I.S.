"""Tools que corren en el servidor y no necesitan cuentas: fecha/hora y tiempo (Open-Meteo)."""

from __future__ import annotations

import unicodedata
from datetime import date, datetime
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

    def run(ctx: ToolContext) -> str:
        now = datetime.now(tz)
        ctx.cards.append({"kind": "clock", "time": f"{now:%H:%M}",
                          "date": f"{DAYS[now.weekday()].capitalize()} {now.day} de {MONTHS[now.month - 1]} de {now.year}"})
        return format_datetime(now)

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
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,is_day",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": days,
            },
        )
        resp.raise_for_status()
        return resp.json()


MAX_FORECAST_DAYS = 14


def weather_card(ctx: ToolContext, where: str, cur: dict, d: dict) -> None:
    """Datos exactos para la tarjeta del tiempo del HUD (iconos y grados salen de aqui, no del texto del modelo)."""
    def num(value):
        return None if value is None else round(float(value))

    days = []
    for i, day in enumerate(d.get("time", [])):
        code = d["weather_code"][i]
        days.append({
            "date": day, "label": "Hoy" if i == 0 else ("Mañana" if i == 1 else _weekday_label(day)),
            "code": code, "text": WEATHER_CODES.get(code, "desconocido"),
            "min": num(d["temperature_2m_min"][i]), "max": num(d["temperature_2m_max"][i]),
            "rain": d.get("precipitation_probability_max", [None] * (i + 1))[i],
        })
    ctx.cards.append({
        "kind": "weather", "title": where,
        "now": {"temp": num(cur["temperature_2m"]), "feels": num(cur["apparent_temperature"]),
                "code": cur["weather_code"], "text": WEATHER_CODES.get(cur["weather_code"], "desconocido"),
                "wind": num(cur["wind_speed_10m"]), "day": cur.get("is_day", 1) != 0},
        "days": days,
    })


def _weekday_label(day: str) -> str:
    """ "2026-10-01" -> "Jueves 1" (el modelo no tiene que calcular qué día de la semana es)."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{DAYS[d.weekday()].capitalize()} {d.day}"


def weather_tool(
    default_city: str, api: OpenMeteo | None = None, home_coords: tuple[float, float] | None = None
) -> Tool:
    api = api or OpenMeteo()

    def run(ctx: ToolContext, city: str = "", days: int = 3) -> str:
        try:
            days = max(1, min(MAX_FORECAST_DAYS, int(days)))
        except (TypeError, ValueError):
            days = 3
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
        weather_card(ctx, where, cur, d)
        for i, day in enumerate(d["time"]):
            label = "Hoy" if i == 0 else ("Mañana" if i == 1 else _weekday_label(day))
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
            "Tiempo actual y previsión día a día (hasta 14 días). Si el usuario no dice ciudad, deja 'city' vacío "
            "y se usará su ciudad por defecto. Elige 'days' según lo que pregunte: hoy o ahora = 1, mañana = 2, "
            "esta semana o los próximos días = 7, el fin de semana = los días que faltan hasta el domingo, "
            "la semana que viene = 14. Cuenta el resumen de todos los días que pidió, no solo el de hoy, y di "
            "siempre las temperaturas en grados (mínima y máxima) y la probabilidad de lluvia si es alta. "
            "El HUD enseña la tarjeta del tiempo con iconos y todos los datos."
        ),
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Ciudad, p. ej. 'Madrid'. Vacío = ciudad del usuario."},
                "days": {"type": "integer", "minimum": 1, "maximum": 14,
                         "description": "Días de previsión desde hoy (1 = solo hoy, 7 = esta semana). Por defecto 3."},
            },
        },
        fn=run,
    )
