"""Planes de varios pasos, preguntar antes de lanzar y preguntar sobre un informe ya hecho."""

from __future__ import annotations

import pytest

from jarvis.activity import ActivityLog
from jarvis.agents import AgentTeam, agent_tools, needs_detail
from jarvis.plans import Planner, parse_steps, plan_tool
from jarvis.tools.registry import Tool, ToolContext, ToolError
from tests.test_agents import fake_search
from tests.test_tools import ScriptedLLM


def team_with(script, **kw):
    return AgentTeam(script.llm(), {"web_search": fake_search()}, **kw)


# --- preguntar antes de lanzar ------------------------------------------------

@pytest.mark.parametrize("agent,task,pregunta", [
    ("compras", "monitor 4k", True),
    ("compras", "un monitor 4k de menos de 300 € para programar", False),
    ("investigador", "impresoras", True),
    ("investigador", "cómo funcionan las impresoras 3D de resina", False),
])
def test_needs_detail(agent, task, pregunta):
    assert (needs_detail(agent, task) is not None) is pregunta


def test_la_conversacion_pregunta_antes_de_gastar_un_agente():
    activity = ActivityLog()
    team = team_with(ScriptedLLM(["RESUMEN: ok.\n# x"]), activity=activity)
    run = {t.name: t for t in agent_tools(team)}["agent_run"]
    out = run.fn(ToolContext(), agent="compras", task="monitor 4k")
    assert "presupuesto" in out and not team.jobs  # no se ha lanzado nada
    ev = [e for e in activity.since(-1) if e["type"] == "agent_ask"]
    assert ev and ev[0]["options"]
    # A la segunda, con el encargo igual, se lanza sin volver a preguntar.
    out2 = run.fn(ToolContext(), agent="compras", task="monitor 4k")
    assert "se ha puesto con ello" in out2 and len(team.jobs) == 1


def test_el_boton_del_hud_no_pregunta():
    team = team_with(ScriptedLLM(["RESUMEN: ok.\n# x"]))
    job, message = team.request("compras", "monitor 4k")
    assert job is not None and "se ha puesto con ello" in message


def test_refresh_no_pregunta():
    team = team_with(ScriptedLLM(["RESUMEN: ok.\n# x"]))
    assert team.request("compras", "monitor 4k", refresh=True, ask=True)[0] is not None


# --- planes -------------------------------------------------------------------

def test_parse_steps_cambia_un_agente_que_no_existe():
    raw = '{"pasos": [{"agente": "compras", "tarea": "compara filamentos PETG"}, {"agente": "nadie", "tarea": "busca opiniones de cada uno"}, {"agente": "compras", "tarea": "no"}]}'
    steps = parse_steps(raw, ["compras", "investigador"], "investigador")
    assert steps == [{"agent": "compras", "task": "compara filamentos PETG"},
                     {"agent": "investigador", "task": "busca opiniones de cada uno"}]


def test_parse_steps_sin_json():
    assert parse_steps("no hay json aquí", ["compras"], "compras") == []


def test_un_plan_reparte_y_junta_un_solo_informe(tmp_path):
    script = ScriptedLLM([
        '{"pasos": [{"agente": "compras", "tarea": "compara filamentos PETG baratos"},'
        ' {"agente": "compras", "tarea": "mira opiniones de los dos mejores"}]}',
        "RESUMEN: El PETG más barato es el de Filamentor.\n# Precios\n| Marca | Precio |\n|---|---|\n| Filamentor | 6,50 € |",
        "RESUMEN: Buenas opiniones del de Filamentor.\n# Opiniones\nLa gente lo recomienda.",
        "RESUMEN: Compra el PETG de Filamentor a 6,50 €.\n# Recomendación\nEl de Filamentor: 6,50 € y buenas opiniones.",
    ])
    activity = ActivityLog()
    team = team_with(script, activity=activity)
    planner = Planner(team, script.llm())
    plan = planner.start("qué PETG compro", background=False)
    assert plan.state == "terminado"
    assert [s["agent"] for s in plan.steps] == ["compras", "compras"]
    assert all(s["state"] == "terminado" for s in plan.steps)
    assert "Filamentor" in plan.report and plan.summary.startswith("Compra el PETG")
    tipos = [e["type"] for e in activity.since(-1)]
    assert "plan_start" in tipos and "plan_done" in tipos


def test_un_plan_de_un_solo_paso_no_gasta_en_juntar():
    script = ScriptedLLM([
        '{"pasos": [{"agente": "compras", "tarea": "compara filamentos PETG baratos"}]}',
        "RESUMEN: El más barato es el de Filamentor.\n# Precios\nFilamentor: 6,50 €",
    ])
    team = team_with(script)
    plan = Planner(team, script.llm()).start("qué PETG compro", background=False)
    assert plan.state == "terminado" and "Filamentor" in plan.report
    assert len(script.steps) == 0  # solo dos llamadas: repartir y el paso


def test_no_hay_dos_planes_a_la_vez():
    script = ScriptedLLM(['{"pasos": [{"agente": "compras", "tarea": "compara filamentos PETG"}]}',
                          "RESUMEN: ok.\n# x"])
    team = team_with(script)
    planner = Planner(team, script.llm())
    planner.start("algo", background=False)
    planner.plans[1].state = "trabajando"
    with pytest.raises(ToolError):
        planner.start("otra cosa")


def test_la_tool_del_plan():
    script = ScriptedLLM(['{"pasos": [{"agente": "compras", "tarea": "compara filamentos PETG"}]}', "RESUMEN: ok.\n# x"])
    planner = Planner(team_with(script), script.llm())
    tool = plan_tool(planner)
    assert isinstance(tool, Tool)
    with pytest.raises(ToolError):
        tool.fn(ToolContext(), task="")


# --- preguntar sobre un informe ya hecho ---------------------------------------

def test_about_job_mete_el_informe_y_prohibe_buscar():
    from tests.test_core import make_assistant

    assistant = make_assistant()
    script = ScriptedLLM(["RESUMEN: listo.\n# Precios\n| Marca | Precio |\n|---|---|\n| Filamentor | 6,50 € |"])
    assistant.team = team_with(script)
    job = assistant.team.start("compras", "filamentos PETG baratos para imprimir", background=False)
    texto = assistant.about_job(job.id)
    assert "Filamentor" in texto and "no lances ningún agente".replace("ú", "u") in texto.replace("ú", "u")
    assert assistant.about_job(999) == ""
