"""Ahorro de tokens: herramientas relevantes y respuestas directas sin LLM.

1. Herramientas relevantes: cada pregunta manda al LLM la descripcion de todas las herramientas (varios miles de
   tokens). Por palabras clave se eligen solo los grupos que vienen a cuento («¿qué tiempo hace?» -> tiempo y hora).
   Si la pregunta no encaja en ningun grupo, se mandan todas: nunca se queda sin la que necesita. En una pregunta
   corta de seguimiento («¿y mañana?») se mantienen las del turno anterior.
2. Respuestas directas: lo trivial (la hora, el tiempo, la agenda, pausar la musica...) se contesta llamando a la
   herramienta y montando la frase aqui, sin LLM: 0 tokens y mas rapido. Solo frases cortas y claras; lo demas va
   al LLM como siempre.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .tools import ToolContext, ToolRegistry

# grupo -> (palabras que lo activan, herramientas del grupo). Una herramienta que no esta en ningun grupo se manda
# siempre (asi las nuevas o las personalizadas no desaparecen).
GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tiempo": (r"tiempo|llover|llueve|lloviendo|lluvia|temperatura\w*|grados|calor|frio|sol|soleado|nublad\w*|"
               r"prevision|paraguas|viento|nieve|nieva|tormenta\w*|clima|abrigo", ("get_weather",)),
    "agenda": (r"agenda|evento\w*|cita\w*|calendario|reunion\w*|apunta\w*|tengo (?:algo|hoy|manana|el|la|esta)|"
               r"que tengo|quedar|quedada|cumple\w*|dentista|medico|semana|finde|fin de semana",
               ("calendar_agenda", "calendar_add")),
    "recordatorios": (r"recuerdame|recordatorio\w*|avisame|alarma\w*|despiertame|no se me olvide",
                      ("reminder_set", "reminder_list", "reminder_cancel")),
    "musica": (r"music\w*|cancion\w*|spotify|suena|sonando|pausa|siguiente|anterior|volumen|playlist|"
               r"lista de reproduccion|disco|album\w*|artista\w*|grupo|pon|ponme|escuchar|reanuda|aleatorio",
               ("spotify_play", "spotify_control", "spotify_now_playing", "pc_media")),
    "pc": (r"pc|ordenador|abre|abreme|youtube|navegador|temporizador|cuenta atras|volumen",
           ("pc_open_app", "pc_open_url", "pc_volume", "pc_timer", "pc_media")),
    "nas": (r"truenas|nas|servidor|reinicia\w*|app|apps|aplicacion\w*|plex|jellyfin|disco|discos|almacenamiento|"
            r"pool|contenedor\w*|docker", ("truenas_status", "truenas_app_restart")),
    "casa": (r"casa|luz|luces|enciende|apaga|encender|apagar|persiana\w*|calefaccion|aire|termostato|enchufe\w*|"
             r"sensor\w*|camara\w*|puerta\w*|alarma|sobremesa",
             ("home_status", "home_control", "home_camera", "wake_on_lan")),
    "web": (r"busca\w*|internet|noticia\w*|titulares|actualidad|quien|que es|que son|cuando|donde|por que|"
            r"precio\w*|cuesta|cuanto vale|resultado\w*|partido|gano|wikipedia|informacion|explica\w*|historia",
            ("web_search", "wikipedia", "news")),
    "conversion": (r"convierte|pasa a|cuanto son|cuantos son|dolar\w*|euro\w*|libra\w*|milla\w*|kilometro\w*|"
                   r"kilo\w*|onza\w*|pulgada\w*|fahrenheit|celsius|divisa\w*|cambio", ("convert",)),
    "memoria": (r"recuerda\w*|acuerdas|olvida\w*|sabes de mi|mi nombre|me llamo|prefiero|me gusta|mi|mis",
                ("memory_save", "memory_search", "memory_update", "memory_forget")),
    "recuerdos": (r"dijiste|dije|hablamos|comentaste|comentamos|acuerdas|acuerdo|recuerdas|quedamos|"
                  r"el otro dia|la otra vez|hace (?:unos |unas )?(?:dias|semanas|meses)|aquello", ("recall",)),
    "notas": (r"nota|notas|obsidian|apunte\w*|diario|anota\w*|boveda", (
        "obsidian_search", "obsidian_read", "obsidian_create_note", "obsidian_append", "obsidian_daily_note")),
    "agentes": (r"investiga\w*|a fondo|compara\w*|agente\w*|cliente\w*|leads?|informe\w*|redacta\w*|audita\w*|"
                r"auditoria|seguridad|claude|encarga\w*|compra\w*|mejor opcion", (
                    "agent_run", "agent_status", "agent_create", "agent_delete", "leads_list", "lead_update",
                    "security_audit", "delegate_claude")),
    "vista": (r"mira|ves|ver|camara|foto|esto|que es esto", ("camera_look",)),
}
ALWAYS = ("get_datetime",)
FOLLOW_UP_WORDS = 6  # pregunta corta de seguimiento: se mantienen las herramientas del turno anterior
_RULES = {name: re.compile(rf"\b(?:{words})\b") for name, (words, _) in GROUPS.items()}
_GROUPED = {t for _, tools in GROUPS.values() for t in tools}


def plain(text: str) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def groups_for(text: str) -> set[str]:
    p = f" {plain(text)} "
    return {name for name, rule in _RULES.items() if rule.search(p)}


def select_specs(specs: list[dict], text: str, previous: set[str] | None = None,
                 keep: set[str] | None = None) -> tuple[list[dict], set[str]]:
    """(herramientas a mandar, grupos elegidos). Sin grupos claros, todas."""
    groups = groups_for(text)
    if previous and len(plain(text).split()) <= FOLLOW_UP_WORDS:
        groups |= previous
    if not groups:
        return specs, set()
    wanted = {t for g in groups for t in GROUPS[g][1]} | set(ALWAYS) | (keep or set())
    chosen = [s for s in specs if s["function"]["name"] in wanted or s["function"]["name"] not in _GROUPED]
    return chosen, groups


# --- respuestas directas ---------------------------------------------------------------------------------------


@dataclass
class Direct:
    reply: str
    tool: str


_WHEN = r"(?: (?P<when>hoy|ahora|manana|esta semana|los proximos dias|la semana que viene|el finde|este finde))?"
_PATTERNS: list[tuple[re.Pattern, str]] = [(re.compile(p), kind) for p, kind in [
    (r"^(?:que hora es|que hora tienes|dime la hora|me dices la hora|la hora)$", "hora"),
    (r"^(?:que dia es hoy|a que dia estamos|que fecha es hoy|dime la fecha|que dia es|a cuanto estamos)$", "fecha"),
    (r"^(?:que tiempo (?:hace|hara|va a hacer)|como esta el tiempo|que tal el tiempo|el tiempo|va a llover|"
     r"llovera|hace frio|hace calor)" + _WHEN + "$", "tiempo"),
    (r"^(?:que tengo|tengo algo|que hay en (?:mi|la) agenda|mi agenda|la agenda|que tengo en (?:la|mi) agenda)"
     r"(?: para)?" + _WHEN + "$", "agenda"),
    (r"^(?:que recordatorios tengo|mis recordatorios|recordatorios pendientes|que recordatorios hay|"
     r"tengo recordatorios)$", "recordatorios"),
    (r"^(?:pausa|pausa la musica|para la musica|pon pausa|deten la musica|para|quita la musica)$", "pausa"),
    (r"^(?:siguiente|siguiente cancion|pasa de cancion|pasa la cancion|salta la cancion|otra cancion)$", "siguiente"),
    (r"^(?:anterior|cancion anterior|la anterior|vuelve a la anterior)$", "anterior"),
    (r"^(?:reanuda|reanuda la musica|continua|sigue con la musica|quita la pausa|dale al play|play)$", "reanudar"),
    (r"^(?:que suena|que cancion es esta|que esta sonando|que cancion suena|que es esto que suena)$", "suena"),
    (r"^(?:pon el volumen|volumen|sube el volumen|baja el volumen)(?: al| a)? (?P<vol>\d{1,3})(?: por ciento)?$",
     "volumen"),
]]


def _days_for(when: str) -> tuple[str, int]:
    """(day para la agenda, dias de prevision del tiempo)."""
    if when == "manana":
        return "mañana", 2
    if when in ("esta semana", "los proximos dias", "el finde", "este finde"):
        return "semana", 7
    if when == "la semana que viene":
        return "semana", 14
    return "hoy", 1


def match(text: str) -> tuple[str, dict[str, str]] | None:
    p = plain(text).removeprefix("jarvis ").removesuffix(" jarvis").removesuffix(" por favor")
    for rule, kind in _PATTERNS:
        m = rule.match(p)
        if m:
            return kind, {k: v for k, v in m.groupdict().items() if v}
    return None


def _card(ctx: ToolContext, kind: str) -> dict[str, Any] | None:
    return next((c for c in reversed(ctx.cards) if c.get("kind") == kind), None)


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def _weather_reply(card: dict, days: int) -> str:
    now, forecast = card.get("now") or {}, card.get("days") or []
    if days == 1 and forecast:
        d = forecast[0]
        out = f"Ahora {now.get('text', '')}, {now.get('temp')} grados. Hoy entre {d['min']} y {d['max']}"
        if (d.get("rain") or 0) >= 40:
            out += f", con un {d['rain']} % de probabilidad de lluvia"
        return out + "."
    if days == 2 and len(forecast) > 1:
        d = forecast[1]
        out = f"Mañana {d['text']}, entre {d['min']} y {d['max']} grados"
        if (d.get("rain") or 0) >= 30:
            out += f", con un {d['rain']} % de probabilidad de lluvia"
        return out + "."
    if not forecast:
        return "No tengo la previsión ahora mismo."
    lo, hi = min(d["min"] for d in forecast), max(d["max"] for d in forecast)
    wet = [d["label"].split()[0].lower() if d["label"] not in ("Hoy", "Mañana") else d["label"].lower()
           for d in forecast if (d.get("rain") or 0) >= 50]
    out = f"Los próximos {len(forecast)} días, entre {lo} y {hi} grados"
    out += f"; lluvia probable {_join(wet)}." if wet else ", sin lluvia a la vista."
    return out


def _agenda_reply(card: dict, day: str) -> str:
    events = card.get("events") or []
    span = {"hoy": "hoy", "mañana": "mañana"}.get(day, "estos días")
    if not events:
        return f"No tienes nada {span}."
    parts = [f"{'todo el día' if not e['time'] else 'a las ' + e['time']} {e['title']}"
             + (f" ({e['day'].lower()})" if day == "semana" else "") for e in events[:6]]
    more = f" y {len(events) - 6} más" if len(events) > 6 else ""
    n = len(events)
    return f"{span.capitalize()} tienes {n} {'evento' if n == 1 else 'eventos'}: {_join(parts)}{more}."


def direct_answer(text: str, tools: ToolRegistry | None, ctx: ToolContext) -> Direct | None:
    """La respuesta sin LLM, o None si la pregunta no es trivial o falta la herramienta."""
    found = match(text) if tools else None
    if not found:
        return None
    kind, groups = found
    names = set(tools.names())

    def run(name: str, **args) -> str | None:
        if name not in names:
            return None
        import json

        return tools.execute(name, json.dumps(args), ctx)

    if kind in ("hora", "fecha"):
        if run("get_datetime") is None or not (card := _card(ctx, "clock")):
            return None
        reply = f"Son las {card['time']}." if kind == "hora" else f"Hoy es {card['date'].lower()}."
        return Direct(reply, "get_datetime")
    if kind == "tiempo":
        _, days = _days_for(groups.get("when", ""))
        result = run("get_weather", days=days)
        if result is None:
            return None
        card = _card(ctx, "weather")
        return Direct(_weather_reply(card, days) if card else _error(result), "get_weather")
    if kind == "agenda":
        day, _ = _days_for(groups.get("when", ""))
        result = run("calendar_agenda", day=day, days=7 if day == "semana" else 1)
        if result is None:
            return None
        card = _card(ctx, "agenda")
        return Direct(_agenda_reply(card, day) if card else _error(result), "calendar_agenda")
    if kind == "recordatorios":
        result = run("reminder_list")
        if result is None:
            return None
        card = _card(ctx, "reminders")
        if not card:
            return Direct(_error(result), "reminder_list")
        items = card["items"]
        if not items:
            return Direct("No tienes recordatorios pendientes.", "reminder_list")
        parts = [f"{r['when']}, {r['text']}" for r in items[:5]]
        return Direct(f"Tienes {len(items)}: {_join(parts)}.", "reminder_list")
    if kind == "suena":
        result = run("spotify_now_playing")
        if result is None:
            return None
        card = _card(ctx, "music")
        return Direct(f"Suena {card['title']} de {card['artist']}." if card else _error(result), "spotify_now_playing")
    action = {"pausa": "pausa", "siguiente": "siguiente", "anterior": "anterior", "reanudar": "reanudar",
              "volumen": "volumen"}[kind]
    args: dict[str, Any] = {"action": action}
    if kind == "volumen":
        args["value"] = min(100, int(groups["vol"]))
    result = run("spotify_control", **args)
    if result is None:
        return None
    if result.startswith("ERROR"):
        return Direct(_error(result), "spotify_control")
    said = {"pausa": "Pausado.", "siguiente": "Siguiente canción.", "anterior": "Canción anterior.",
            "reanudar": "Sigo con la música.", "volumen": f"Volumen al {args.get('value')} %."}
    return Direct(said[kind], "spotify_control")


def _error(result: str) -> str:
    return f"No he podido: {result.removeprefix('ERROR: ')}" if result.startswith("ERROR") else result
