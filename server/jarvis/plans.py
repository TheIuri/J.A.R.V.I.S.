"""Planes de varios pasos: un encargo grande se reparte entre varios agentes.

"Organízame la compra de material para el proyecto X" no es un encargo para un solo agente: hay que
mirar qué hace falta, comparar precios y ordenarlo. El planificador parte la petición en 2 o 3 pasos,
se los da al agente que toca (uno detrás de otro, para no saturar), y al final junta todo en un solo
informe con un modelo. El usuario ve un único resultado, no tres informes sueltos.

El reparto lo propone el modelo, pero solo puede elegir entre los agentes que existen: cualquier otro
nombre se cambia por el investigador.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .agents import MAX_NOTE_CHARS, MAX_REPORT_CHARS, AgentTeam, short_summary, split_report
from .llm import FallbackLLM, LLMError
from .obsidian import VaultError
from .tools.registry import Tool, ToolContext, ToolError

log = logging.getLogger("jarvis.plans")

MAX_STEPS = 3
MAX_STEP_CHARS = 2600  # de cada informe, lo que entra en el resumen final
FOLDER = "JARVIS/Planes"

SPLIT_PROMPT = """Eres el planificador de JARVIS. Parte la peticion del usuario en los pasos minimos para
resolverla bien, cada uno para el agente que mejor encaje. Agentes disponibles:
@AGENTS@

Reglas:
- De 2 a @MAX@ pasos. Si con uno solo basta, devuelve un solo paso.
- Cada paso, una tarea concreta y completa que ese agente pueda hacer solo, con el detalle que dio el usuario.
- Los pasos van en orden: uno puede dar por hecho lo que encontro el anterior.
- No inventes datos del usuario.
Devuelve SOLO un JSON: {"pasos": [{"agente": "<clave>", "tarea": "<que tiene que hacer>"}]}"""

MERGE_PROMPT = """Junta estos informes de varios agentes en UN SOLO informe para el usuario, en espanol de Espana.
No repitas lo mismo dos veces, ordena la informacion como le sirva a el y manten las tablas, los precios y los
enlaces tal cual (no inventes ninguno; si un dato no esta, no lo pongas). Formato exacto:
RESUMEN: <una o dos frases cortas, maximo 200 caracteres, sin markdown>
# <titulo>
<informe en markdown, maximo 6000 caracteres>"""


@dataclass
class Plan:
    id: int
    task: str
    started: datetime
    steps: list[dict[str, Any]] = field(default_factory=list)  # [{"agent", "task", "job", "state", "summary"}]
    state: str = "trabajando"  # trabajando | terminado | error
    summary: str = ""
    report: str = ""
    note: str = ""


def parse_steps(text: str, agents: list[str], fallback: str) -> list[dict[str, str]]:
    """El JSON del modelo -> pasos validos. Un agente que no existe pasa al de respaldo."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for item in (data.get("pasos") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        task = " ".join(str(item.get("tarea") or "").split())[:300]
        agent = str(item.get("agente") or "").strip()
        if len(task) < 8:
            continue
        out.append({"agent": agent if agent in agents else fallback, "task": task})
    return out[:MAX_STEPS]


class Planner:
    def __init__(self, team: AgentTeam, llm: FallbackLLM, timezone: str = "Europe/Madrid"):
        self.team = team
        self.llm = llm
        self.tz = ZoneInfo(timezone)
        self.plans: dict[int, Plan] = {}
        self._next = 1
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return any(p.state == "trabajando" for p in self.plans.values())

    def split(self, task: str) -> list[dict[str, str]]:
        agents = self.team.available
        if not agents:
            raise ToolError("no hay agentes disponibles")
        listing = "\n".join(f"- {k}: {self.team.specs[k].description}" for k in agents)
        prompt = SPLIT_PROMPT.replace("@AGENTS@", listing).replace("@MAX@", str(MAX_STEPS))
        fallback = "investigador" if "investigador" in agents else agents[0]
        try:
            reply = self.llm.chat([{"role": "system", "content": prompt}, {"role": "user", "content": task}],
                                  patient=True)
        except LLMError as exc:
            raise ToolError(f"no he podido repartir el encargo: {exc}") from exc
        steps = parse_steps(reply.text, agents, fallback)
        return steps or [{"agent": fallback, "task": task}]

    def start(self, task: str, background: bool = True) -> Plan:
        task = " ".join(task.split())[:300]
        if not task:
            raise ToolError("¿qué quieres que organice?")
        with self._lock:
            if self.busy:
                raise ToolError("ya hay un plan en marcha; espera a que termine")
            plan = Plan(self._next, task, datetime.now(self.tz))
            self.plans[plan.id] = plan
            self._next += 1
        if background:
            threading.Thread(target=self._run, args=(plan,), name=f"plan-{plan.id}", daemon=True).start()
        else:
            self._run(plan)
        return plan

    def _trace(self, kind: str, plan: Plan, **data) -> None:
        if self.team.activity:
            self.team.activity.emit(kind, agent="plan", label="El plan", plan=plan.id, **data)

    def _run(self, plan: Plan) -> None:
        try:
            plan.steps = [dict(s, job=0, state="pendiente", summary="") for s in self.split(plan.task)]
            self._trace("plan_start", plan, task=plan.task,
                        steps=[{"agent": s["agent"], "task": s["task"]} for s in plan.steps])
            log.info("plan %d: %d pasos", plan.id, len(plan.steps))
            done = []
            for step in plan.steps:
                step["state"] = "trabajando"
                job = self.team.start(step["agent"], step["task"], background=False)
                step.update(job=job.id, state=job.state, summary=job.summary)
                if job.state == "terminado" and job.report:
                    label = self.team.specs[job.agent].label
                    done.append(f"## {label} · {step['task']}\n{job.report[:MAX_STEP_CHARS]}")
            if not done:
                raise ToolError("ningún paso del plan ha dado resultado")
            plan.summary, plan.report = self._merge(plan, done)
            plan.note = self._save(plan)
            plan.state = "terminado"
        except Exception as exc:
            plan.state = "error"
            plan.summary = str(exc) if isinstance(exc, (ToolError, LLMError)) else type(exc).__name__
            log.exception("plan %d fallo", plan.id)
            self._trace("plan_error", plan, detail=plan.summary[:200])
            if self.team.board:
                self.team.board.post("warning", "plan", f"No he podido terminar el plan: {short_summary(plan.task)}",
                                     key=f"plan:{plan.id}:error")
            return
        self._trace("plan_done", plan, task=plan.task, summary=plan.summary, report=plan.report, note=plan.note,
                    steps=[{"agent": s["agent"], "task": s["task"], "job": s["job"], "state": s["state"]}
                           for s in plan.steps])
        if self.team.board:
            self.team.board.post("info", "plan", f"Plan terminado. {plan.summary}", key=f"plan:{plan.id}:done")
        if self.team.on_done:  # la conversacion se entera del informe final
            try:
                self.team.on_done("El plan", _AsJob(plan))
            except Exception:
                log.exception("no se pudo pasar el plan a la conversacion")
        log.info("plan %d: terminado (%d pasos)", plan.id, len(plan.steps))

    def _merge(self, plan: Plan, reports: list[str]) -> tuple[str, str]:
        if len(reports) == 1:  # un solo paso: no hay nada que juntar (ni tokens que gastar)
            return split_report(reports[0].split("\n", 1)[-1])[0][:200] or plan.summary, reports[0]
        text = f"Peticion del usuario: {plan.task}\n\n" + "\n\n".join(reports)
        try:
            reply = self.llm.chat([{"role": "system", "content": MERGE_PROMPT}, {"role": "user", "content": text}],
                                  patient=True)
        except LLMError as exc:
            log.warning("plan %d: no se pudo juntar (%s); se entregan los pasos tal cual", plan.id, exc)
            return f"Plan terminado con {len(reports)} pasos.", "\n\n".join(reports)[:MAX_REPORT_CHARS]
        summary, report = split_report(reply.text)
        return short_summary(summary), report[:MAX_REPORT_CHARS]

    def _save(self, plan: Plan) -> str:
        if not self.team.vault:
            return ""
        pasos = "\n".join(f"{i}. {s['task']} ({s['agent']})" for i, s in enumerate(plan.steps, 1))
        header = f"> Plan de JARVIS · {plan.started:%d/%m/%Y %H:%M}\n\n## Pasos\n{pasos}\n\n"
        try:
            return self.team.vault.create(f"{plan.started:%Y-%m-%d} Plan {plan.task}"[:120], header + plan.report,
                                          FOLDER, check_secrets=False, max_chars=MAX_NOTE_CHARS)
        except VaultError as exc:
            log.warning("plan %d: no se pudo guardar: %s", plan.id, exc)
            return ""


class _AsJob:
    """Un plan con la pinta de un encargo, para reutilizar lo que ya sabe hacer la conversacion."""

    def __init__(self, plan: Plan):
        self.id = plan.id
        self.agent = "plan"
        self.topic = plan.task
        self.summary = plan.summary
        self.report = plan.report
        self.note = plan.note
        self.state = plan.state


def plan_tool(planner: Planner) -> Tool:
    def run(_ctx: ToolContext, task: str) -> str:
        plan = planner.start(task)
        return (f"Plan {plan.id} en marcha: lo reparto entre varios agentes y te doy un único informe al terminar. "
                "Avisaré cuando esté.")

    return Tool(
        name="agent_plan",
        description=(
            "Para un encargo grande que necesita varias cosas a la vez (buscar, comparar y organizar): lo parte en "
            f"hasta {MAX_STEPS} pasos, se los reparte a los agentes que tocan y junta todo en un solo informe. "
            "Para algo que hace un solo agente, usa agent_run; tarda bastante más y gasta más, así que úsalo solo "
            "cuando de verdad haya varias partes."
        ),
        parameters={
            "type": "object",
            "properties": {"task": {"type": "string", "description": "La petición completa, con todo el detalle"}},
            "required": ["task"],
        },
        fn=run,
    )
