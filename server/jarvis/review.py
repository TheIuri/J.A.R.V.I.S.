"""El repaso semanal: JARVIS mira sus propios fallos y propone como mejorar.

Una vez por semana, de madrugada, junta lo de los ultimos siete dias (los pulgares, las preguntas que
se repiten, las herramientas que dan error) y saca propuestas. Dos tipos:

- Las que salen de contar, sin modelo: atajos para lo que preguntas siempre (ver routes.py).
- Las que salen de leer lo que fallo: reglas nuevas para la memoria y agentes que merecerian existir.
  Eso si necesita una llamada al modelo, una a la semana.

Nada se aplica solo. Todo queda como propuesta, con su motivo, y tu apruebas o descartas desde el HUD.
Un asistente que se cambia las reglas sin permiso se estropea sin que te enteres, y ademas no habria
forma de saber que cambio ni de volver atras.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .llm import FallbackLLM, LLMError
from .obsidian import VaultError
from .routes import Route, candidates

log = logging.getLogger("jarvis.review")

FOLDER = "JARVIS/Repasos"
MAX_PROPOSALS = 12
KINDS = ("atajo", "regla", "agente", "aviso")

PROMPT = """Eres JARVIS repasando tu propia semana para mejorar. Te paso lo que fallo y lo que se repite.
Saca propuestas concretas y accionables. Tipos:
- "regla": algo que deberias recordar siempre para no repetir un fallo (frase corta, en imperativo).
- "agente": un agente especializado que merece existir porque ese tipo de encargo se repite. Pon nombre,
  descripcion de una linea e instrucciones detalladas.
- "aviso": algo que el usuario deberia saber o configurar (una herramienta que falla siempre, algo sin configurar).
Devuelve SOLO un JSON:
{"resumen": "<2 o 3 frases sobre como ha ido la semana>",
 "propuestas": [{"tipo": "regla|agente|aviso", "titulo": "<frase corta>", "motivo": "<por que, citando el dato>",
                 "texto": "<la regla, o el aviso>", "nombre": "<solo agente>", "descripcion": "<solo agente>",
                 "instrucciones": "<solo agente>"}]}
Como mucho 5 propuestas, solo las que de verdad cambien algo. Si la semana fue bien, deja la lista vacia.
No propongas nada que ya este en "Lo que ya recuerda". En espanol de Espana."""


@dataclass
class Proposal:
    id: str
    kind: str
    title: str
    why: str
    data: dict[str, Any] = field(default_factory=dict)
    created: str = ""
    state: str = "pendiente"  # pendiente | aprobada | descartada


class Proposals:
    """Las propuestas pendientes, en disco: el usuario las aprueba o las descarta desde el HUD."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.items: list[Proposal] = []
        if self.path and self.path.exists():
            try:
                self.items = [Proposal(**raw) for raw in json.loads(self.path.read_text(encoding="utf-8"))]
            except (OSError, ValueError, TypeError):
                log.warning("no se han podido leer las propuestas")

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([asdict(p) for p in self.items], ensure_ascii=False, indent=1),
                             encoding="utf-8")

    def pending(self) -> list[Proposal]:
        return [p for p in self.items if p.state == "pendiente"]

    def add(self, kind: str, title: str, why: str, data: dict | None = None) -> Proposal:
        if kind not in KINDS:
            raise ValueError(f"tipo de propuesta desconocido: {kind}")
        item = Proposal(uuid.uuid4().hex[:8], kind, title[:120], why[:400], data or {},
                        datetime.now().isoformat(timespec="minutes"))
        with self._lock:
            self.items.append(item)
            self.items = self.items[-60:]
            self._save()
        return item

    def get(self, ident: str) -> Proposal | None:
        return next((p for p in self.items if p.id == ident), None)

    def close(self, ident: str, state: str) -> Proposal | None:
        with self._lock:
            item = self.get(ident)
            if item is None:
                return None
            item.state = state
            self._save()
        return item


def parse(text: str) -> tuple[str, list[dict]]:
    """(resumen, propuestas) del JSON del modelo."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return "", []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "", []
    if not isinstance(data, dict):
        return "", []
    out = []
    for item in data.get("propuestas") or []:
        if isinstance(item, dict) and item.get("tipo") in KINDS and str(item.get("titulo", "")).strip():
            out.append(item)
    return str(data.get("resumen") or "")[:600], out[:5]


def week_facts(turns: list[dict], routes: dict[str, Route] | None = None) -> dict[str, Any]:
    """Los numeros de la semana, contados aqui: sin modelo y sin discusion."""
    good = [t for t in turns if t.get("verdict") == "bien"]
    bad = [t for t in turns if t.get("verdict") == "mal"]
    tools = Counter(tool for t in turns for tool in (t.get("tools") or "").split(",") if tool)
    models = Counter(t.get("model") or "?" for t in turns)
    return {
        "turnos": len(turns),
        "bien": len(good),
        "mal": len(bad),
        "fallos": [{"pregunta": t.get("user", "")[:200], "respuesta": t.get("reply", "")[:300],
                    "motivo": t.get("note", "")[:200], "modelo": t.get("model", "")} for t in bad[:10]],
        "herramientas": tools.most_common(8),
        "modelos": models.most_common(6),
        "atajos": [r for r in candidates(turns, routes)][:5],
    }


class Reviewer:
    def __init__(self, llm: FallbackLLM, turn_log, proposals: Proposals, memory=None, vault=None,
                 activity=None, board=None, timezone: str = "Europe/Madrid"):
        self.llm = llm
        self.turn_log = turn_log
        self.proposals = proposals
        self.memory = memory
        self.vault = vault
        self.activity = activity
        self.board = board
        self.tz = ZoneInfo(timezone)

    def known_rules(self) -> list[str]:
        return [m.content for m in self.memory.all("preferencia", limit=30)] if self.memory else []

    def run(self, routes: dict[str, Route] | None = None) -> dict[str, Any]:
        """El repaso de la semana: deja las propuestas pendientes y devuelve el resumen."""
        turns = self.turn_log.since(7) if self.turn_log else []
        facts = week_facts(turns, routes)
        new: list[Proposal] = []
        # 1. Atajos: de contar lo que repites. No hace falta modelo.
        for route in facts["atajos"]:
            new.append(self.proposals.add(
                "atajo", f"Contestar «{route.shape}» sin modelo",
                f"Lo has preguntado {route.times} veces esta semana y siempre acaba en {route.tool}.",
                {"shape": route.shape, "tool": route.tool, "args": route.args, "times": route.times}))
        # 2. Lo que hay que leer: los fallos. Una llamada al modelo a la semana.
        summary = ""
        if facts["mal"] or facts["turnos"] >= 20:
            summary = self._ask(facts, new)
        note = self._save_note(facts, summary, new)
        if self.activity:
            self.activity.emit("review", agent="repaso", label="El repaso semanal", summary=summary,
                               proposals=len(new), bad=facts["mal"], turns=facts["turnos"], note=note)
        if self.board and new:
            self.board.post("info", "repaso", f"Repaso de la semana: {len(new)} propuestas para mejorar.",
                            key=f"review:{datetime.now(self.tz):%Y-%W}")
        log.info("repaso: %d turnos, %d fallos, %d propuestas", facts["turnos"], facts["mal"], len(new))
        return {"summary": summary, "proposals": [asdict(p) for p in new], "facts": facts, "note": note}

    def _ask(self, facts: dict, new: list[Proposal]) -> str:
        body = {
            "turnos": facts["turnos"], "pulgares_arriba": facts["bien"], "pulgares_abajo": facts["mal"],
            "fallos": facts["fallos"], "herramientas_mas_usadas": facts["herramientas"],
            "modelos": facts["modelos"], "lo_que_ya_recuerda": self.known_rules(),
        }
        messages = [{"role": "system", "content": PROMPT},
                    {"role": "user", "content": json.dumps(body, ensure_ascii=False)[:8000]}]
        try:
            reply = self.llm.chat(messages, patient=True)
        except LLMError as exc:
            log.warning("repaso sin propuestas del modelo: %s", exc)
            return ""
        summary, found = parse(reply.text)
        for item in found:
            data = {k: str(item.get(k, ""))[:2000] for k in ("texto", "nombre", "descripcion", "instrucciones")}
            new.append(self.proposals.add(item["tipo"], str(item["titulo"]), str(item.get("motivo", "")), data))
        return summary

    def _save_note(self, facts: dict, summary: str, new: list[Proposal]) -> str:
        if not self.vault:
            return ""
        now = datetime.now(self.tz)
        lines = [f"> Repaso semanal de JARVIS · {now:%d/%m/%Y}", "",
                 f"- Turnos: {facts['turnos']} · pulgar arriba: {facts['bien']} · pulgar abajo: {facts['mal']}"]
        if facts["herramientas"]:
            lines.append("- Herramientas más usadas: "
                         + ", ".join(f"{name} ({n})" for name, n in facts["herramientas"]))
        if summary:
            lines += ["", "## Cómo ha ido", summary]
        if new:
            lines += ["", "## Propuestas (pendientes de tu visto bueno)"]
            lines += [f"- **{p.title}** ({p.kind}): {p.why}" for p in new]
        if facts["fallos"]:
            lines += ["", "## Lo que falló"]
            lines += [f"- «{f['pregunta']}»" + (f" — {f['motivo']}" if f["motivo"] else "") for f in facts["fallos"]]
        try:
            return self.vault.create(f"{now:%Y-%m-%d} Repaso semanal", "\n".join(lines), FOLDER,
                                     check_secrets=False, max_chars=9000)
        except VaultError as exc:
            log.warning("repaso: no se pudo guardar la nota: %s", exc)
            return ""


def apply_proposal(item: Proposal, routes=None, memory=None, team=None) -> str:
    """Aplica una propuesta aprobada. Devuelve que se ha hecho, o lanza ValueError."""
    if item.kind == "atajo":
        if routes is None:
            raise ValueError("los atajos no están disponibles")
        route = Route(item.data["shape"], item.data["tool"], item.data.get("args") or {},
                      int(item.data.get("times", 0)), datetime.now().isoformat(timespec="minutes"))
        routes.add(route)
        return f"Atajo activado: {route.line()}"
    if item.kind == "regla":
        if memory is None:
            raise ValueError("la memoria no está disponible")
        text = (item.data.get("texto") or item.title).strip()
        saved = memory.add(text, "preferencia", source="repaso", confidence=0.7)
        return f"Regla guardada [{saved.id}]: {saved.content}"
    if item.kind == "agente":
        if team is None:
            raise ValueError("los agentes no están disponibles")
        entry = team.create_agent(item.data.get("nombre") or item.title,
                                  item.data.get("descripcion") or item.why,
                                  item.data.get("instrucciones") or "")
        return f"Agente «{entry['name']}» creado."
    return "Aviso leído."  # un aviso solo se lee; aprobarlo es marcarlo como visto
