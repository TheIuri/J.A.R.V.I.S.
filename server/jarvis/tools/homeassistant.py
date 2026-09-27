"""Home Assistant: consultar y controlar la casa por su API REST.

Se activa con HA_URL y HA_TOKEN (token de larga duracion: perfil de usuario en HA -> Seguridad).
Solo dominios seguros: luces, enchufes, ventiladores, persianas, clima, multimedia, escenas y scripts.
Cerraduras y alarmas quedan fuera a proposito. HA_ENTITIES limita aun mas lo que JARVIS ve
(p. ej. "light.,switch.salon,climate."), por prefijo de entity_id.
"""

from __future__ import annotations

import unicodedata

import httpx

from .registry import Tool, ToolContext, ToolError

READ_DOMAINS = ("light", "switch", "fan", "cover", "climate", "media_player", "sensor", "binary_sensor", "scene", "script")
# accion -> (dominios permitidos, servicio)
ACTIONS = {
    "encender": (("light", "switch", "fan", "media_player", "climate"), "turn_on"),
    "apagar": (("light", "switch", "fan", "media_player", "climate"), "turn_off"),
    "alternar": (("light", "switch", "fan"), "toggle"),
    "brillo": (("light",), "turn_on"),
    "temperatura": (("climate",), "set_temperature"),
    "abrir": (("cover",), "open_cover"),
    "cerrar": (("cover",), "close_cover"),
    "activar": (("scene", "script"), "turn_on"),
}
MAX_LIST = 40
# "que luces hay encendidas" -> dominio light
TYPE_WORDS = {
    "luz": "light", "luces": "light", "lampara": "light", "lamparas": "light",
    "enchufe": "switch", "enchufes": "switch", "interruptor": "switch", "interruptores": "switch",
    "ventilador": "fan", "ventiladores": "fan", "persiana": "cover", "persianas": "cover", "toldo": "cover",
    "clima": "climate", "calefaccion": "climate", "termostato": "climate", "aire": "climate",
    "sensor": "sensor", "sensores": "sensor", "tele": "media_player", "television": "media_player",
    "altavoz": "media_player", "altavoces": "media_player", "escena": "scene", "escenas": "scene",
}


def _norm(text: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return " ".join(plain.replace("_", " ").replace(".", " ").split())


class HomeAssistant:
    def __init__(self, url: str, token: str, entities: str = "", client: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.filters = [f.strip() for f in entities.split(",") if f.strip()]
        self._client = client or httpx.Client(timeout=10)
        self._headers = {"Authorization": f"Bearer {token}"}

    def _allowed(self, entity_id: str) -> bool:
        if entity_id.split(".")[0] not in READ_DOMAINS:
            return False
        return not self.filters or any(entity_id.startswith(f) for f in self.filters)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            resp = self._client.request(method, f"{self.url}{path}", headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise ToolError(f"no puedo conectar con Home Assistant ({type(exc).__name__})") from exc
        if resp.status_code == 401:
            raise ToolError("Home Assistant rechaza el token (HA_TOKEN)")
        if resp.status_code >= 400:
            raise ToolError(f"Home Assistant respondió {resp.status_code}")
        return resp

    def states(self) -> list[dict]:
        return [s for s in self._request("GET", "/api/states").json() if self._allowed(s.get("entity_id", ""))]

    def find(self, name: str, domains: tuple[str, ...] | None = None) -> dict:
        """Por entity_id exacto o por nombre visible (sin acentos; vale un trozo del nombre)."""
        states = [s for s in self.states() if not domains or s["entity_id"].split(".")[0] in domains]
        key = _norm(name)
        for s in states:
            if s["entity_id"] == name.strip():
                return s
        exact = [s for s in states if _norm(s.get("attributes", {}).get("friendly_name", "")) == key]
        partial = [s for s in states if key and key in _norm(s.get("attributes", {}).get("friendly_name", "") + " " + s["entity_id"])]
        matches = exact or partial
        if not matches:
            raise ToolError(f"no encuentro '{name}' en Home Assistant")
        if len(matches) > 1 and not exact:
            names = ", ".join(_label(s) for s in matches[:6])
            raise ToolError(f"'{name}' puede ser varias cosas: {names}. Pregunta cuál")
        return matches[0]

    def call(self, domain: str, service: str, data: dict) -> None:
        self._request("POST", f"/api/services/{domain}/{service}", json=data)


def _label(state: dict) -> str:
    return state.get("attributes", {}).get("friendly_name") or state["entity_id"]


def _describe(state: dict) -> str:
    attrs = state.get("attributes", {})
    value = state.get("state", "?")
    unit = attrs.get("unit_of_measurement", "")
    extra = ""
    if state["entity_id"].startswith("light.") and value == "on" and attrs.get("brightness") is not None:
        extra = f", brillo {round(attrs['brightness'] / 255 * 100)}%"
    if state["entity_id"].startswith("climate.") and attrs.get("current_temperature") is not None:
        extra = f", {attrs['current_temperature']} °C (objetivo {attrs.get('temperature', '?')} °C)"
    return f"{_label(state)}: {value}{(' ' + unit) if unit else ''}{extra}"


def ha_tools(ha: HomeAssistant) -> list[Tool]:
    def status(_ctx: ToolContext, query: str = "") -> str:
        if query:
            try:
                return _describe(ha.find(query))
            except ToolError as exc:
                if "varias cosas" in str(exc):
                    raise
        states = ha.states()
        if query:
            key = _norm(query)
            domain = TYPE_WORDS.get(key)
            states = [
                s for s in states
                if (domain and s["entity_id"].startswith(domain + ".")) or key in _norm(s["entity_id"] + " " + _label(s))
            ]
        if not states:
            return "No hay dispositivos que coincidan."
        lines = [_describe(s) for s in states[:MAX_LIST]]
        if len(states) > MAX_LIST:
            lines.append(f"... y {len(states) - MAX_LIST} más (pregunta por algo concreto).")
        return "\n".join(lines)

    def control(_ctx: ToolContext, entity: str, action: str, value: float | None = None) -> str:
        domains, service = ACTIONS[action]
        state = ha.find(entity, domains)
        domain = state["entity_id"].split(".")[0]
        data: dict = {"entity_id": state["entity_id"]}
        if action == "brillo":
            if value is None or not 0 <= value <= 100:
                raise ToolError("el brillo va de 0 a 100")
            data["brightness_pct"] = round(value)
        elif action == "temperatura":
            if value is None or not 5 <= value <= 35:
                raise ToolError("la temperatura debe estar entre 5 y 35 °C")
            data["temperature"] = value
        ha.call(domain, service, data)
        return f"Hecho: {action} {_label(state)}" + (f" ({value:g})" if value is not None else "") + "."

    return [
        Tool(
            name="home_status",
            description=(
                "Estado de la casa en Home Assistant: luces, enchufes, clima, persianas, sensores (temperatura, "
                "humedad...). Con 'query' busca un dispositivo, una habitación o un tipo ('luces', 'salón')."
            ),
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            fn=status,
        ),
        Tool(
            name="home_control",
            description=(
                "Controla un dispositivo de Home Assistant por su nombre: encender/apagar/alternar luces, enchufes y "
                "ventiladores; brillo (0-100); temperatura del clima; abrir/cerrar persianas; activar escenas o scripts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "Nombre del dispositivo (p. ej. 'luz del salón')"},
                    "action": {"type": "string", "enum": sorted(ACTIONS)},
                    "value": {"type": "number", "description": "Brillo 0-100 o temperatura en °C"},
                },
                "required": ["entity", "action"],
            },
            fn=control,
        ),
    ]
