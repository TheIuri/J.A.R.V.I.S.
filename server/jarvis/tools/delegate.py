"""Delegar tareas complejas a Claude Code en el PC del usuario, con su membresia Claude Pro/Max.

Solo se ofrece cuando hablas desde el HUD del PC (es quien la ejecuta) y siempre pide un "si"
antes, porque gasta cupo de la membresia. El resultado vuelve por /api/agent_result: se guarda
en Obsidian y se avisa por el tablon.
"""

from __future__ import annotations

from .registry import Tool, ToolContext, ToolError


def delegate_tool() -> Tool:
    def run(ctx: ToolContext, task: str) -> str:
        task = " ".join(task.split())
        if len(task) < 5:
            raise ToolError("describe la tarea con más detalle")
        ctx.pc_actions.append({"action": "delegate", "agent": "claude", "task": task[:1500]})
        return "Tarea enviada a Claude en tu PC; te aviso cuando termine y lo guardo en Obsidian."

    return Tool(
        name="delegate_claude",
        description=(
            "Encarga una tarea compleja (investigar a fondo, comparar, analizar, redactar algo largo) a Claude, "
            "usando la membresía del usuario desde su PC. Tarda unos minutos y avisa al terminar. Para lo sencillo "
            "responde tú; para investigar sin gastar membresía usa agent_research."
        ),
        parameters={
            "type": "object",
            "properties": {"task": {"type": "string", "description": "La tarea completa, con todo el contexto"}},
            "required": ["task"],
        },
        fn=run,
        pc=True,
        confirm=True,
        describe=lambda args: f"encargar a Claude, con tu membresía, esta tarea: {args.get('task', '')[:120]}",
        timeout_s=5,
    )
