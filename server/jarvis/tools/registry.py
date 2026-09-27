"""Registro de tools. El LLM solo propone una llamada; este codigo decide si se ejecuta.

Reglas de la guia: tools pequenas, tipadas y probables sin LLM; nunca codigo arbitrario;
cada llamada queda en el log de auditoria; lo destructivo necesita confirmacion humana.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

audit = logging.getLogger("jarvis.audit")

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list}


@dataclass
class PendingAction:
    """Accion propuesta por el LLM que espera un "si" del usuario (ver Assistant)."""

    tool: "Tool"
    args: dict[str, Any]
    summary: str


@dataclass
class ToolContext:
    """Estado de un turno: lo que se pide al PC y las apps que el PC permite abrir."""

    pc_apps: list[str] | None = None  # None = no hay cliente de PC capaz de ejecutar acciones
    pc_actions: list[dict[str, Any]] = field(default_factory=list)
    pending: PendingAction | None = None  # accion que necesita confirmacion humana
    cards: list[dict[str, Any]] = field(default_factory=list)  # resultados para mostrar en el HUD


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)
    fn: Callable[..., str]
    pc: bool = False  # se ejecuta en el PC del usuario, no en el servidor
    destructive: bool = False  # borrar, enviar, pagar... nunca se ofrece al LLM
    confirm: bool = False  # se ofrece, pero solo se ejecuta si el usuario dice "si" en el turno siguiente
    describe: Callable[[dict[str, Any]], str] | None = None  # texto de la pregunta de confirmacion
    timeout_s: float = 10.0


class ToolError(Exception):
    """Error esperado de una tool (argumento invalido, servicio caido...). Se devuelve al LLM."""


def _validate(schema: dict[str, Any], args: dict[str, Any]) -> None:
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            raise ToolError(f"falta el argumento obligatorio '{key}'")
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            raise ToolError(f"argumento desconocido '{key}'")
        expected = _TYPES.get(spec.get("type", ""))
        if expected and (not isinstance(value, expected) or (expected is not bool and isinstance(value, bool))):
            raise ToolError(f"'{key}' debe ser de tipo {spec['type']}")
        if "enum" in spec and value not in spec["enum"]:
            raise ToolError(f"'{key}' debe ser uno de {spec['enum']}")
        if "minimum" in spec and value < spec["minimum"] or "maximum" in spec and value > spec["maximum"]:
            raise ToolError(f"'{key}' fuera de rango")


class ToolRegistry:
    def __init__(self, disabled: set[str] | None = None):
        self._tools: dict[str, Tool] = {}
        self._disabled = disabled or set()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool duplicada: {tool.name}")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return [n for n in self._tools if n not in self._disabled]

    def _available(self, ctx: ToolContext) -> list[Tool]:
        # Lo destructivo no se ofrece mientras no exista un flujo de confirmacion humana.
        return [
            t
            for t in self._tools.values()
            if t.name not in self._disabled
            and not t.destructive
            and (not t.pc or ctx.pc_apps is not None)
            and (t.name != "pc_open_app" or ctx.pc_apps)
        ]

    def specs(self, ctx: ToolContext) -> list[dict[str, Any]]:
        """Definiciones en formato OpenAI. Solo se ofrecen las tools que se pueden ejecutar ahora."""
        specs = []
        for t in self._available(ctx):
            params = t.parameters
            if t.name == "pc_open_app":
                params = json.loads(json.dumps(params))
                params["properties"]["app"]["enum"] = ctx.pc_apps
            specs.append({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": params}})
        return specs

    def execute(self, name: str, raw_args: str, ctx: ToolContext) -> str:
        start = time.perf_counter()
        ok, result = False, ""
        try:
            tool = next((t for t in self._available(ctx) if t.name == name), None)
            if tool is None:
                raise ToolError(f"la tool '{name}' no existe o no esta permitida")
            try:
                args = json.loads(raw_args or "{}")
            except json.JSONDecodeError as exc:
                raise ToolError(f"argumentos no son JSON valido: {exc}") from exc
            if not isinstance(args, dict):
                raise ToolError("los argumentos deben ser un objeto JSON")
            _validate(tool.parameters, args)
            if tool.name == "pc_open_app" and args.get("app") not in (ctx.pc_apps or []):
                raise ToolError(f"app no permitida; opciones: {ctx.pc_apps}")
            if tool.confirm:
                summary = tool.describe(args) if tool.describe else f"{tool.name} {args}"
                ctx.pending = PendingAction(tool, args, summary)
                result = (
                    f"PENDIENTE DE CONFIRMACION: {summary}. No lo has hecho todavia. Pregunta al usuario, en una frase, "
                    "si lo confirma; solo se hara si responde que si."
                )
            else:
                result = self._call(tool, ctx, args)
            ok = True
        except ToolError as exc:
            result = f"ERROR: {exc}"
        except Exception as exc:  # una tool rota no debe tumbar el asistente
            audit.exception("tool %s fallo", name)
            result = f"ERROR: fallo interno en la tool ({type(exc).__name__})"
        ms = round((time.perf_counter() - start) * 1000)
        audit.info(
            "tool=%s ok=%s ms=%d args=%s result=%s", name, ok, ms, raw_args, result[:200].replace("\n", " ")
        )
        return result

    def _call(self, tool: Tool, ctx: ToolContext, args: dict[str, Any]) -> str:
        future = self._pool.submit(tool.fn, ctx, **args)
        try:
            return future.result(timeout=tool.timeout_s)
        except FutureTimeout as exc:
            raise ToolError(f"tiempo agotado ({tool.timeout_s:.0f}s)") from exc

    def run_confirmed(self, pending: PendingAction, ctx: ToolContext) -> tuple[bool, str]:
        """Ejecuta una accion que el usuario acaba de confirmar. Devuelve (ok, resultado)."""
        start = time.perf_counter()
        ok, result = False, ""
        try:
            if pending.tool.name in self._disabled:
                raise ToolError(f"la tool '{pending.tool.name}' esta desactivada")
            result = self._call(pending.tool, ctx, pending.args)
            ok = True
        except ToolError as exc:
            result = f"ERROR: {exc}"
        except Exception as exc:
            audit.exception("tool %s fallo", pending.tool.name)
            result = f"ERROR: fallo interno en la tool ({type(exc).__name__})"
        ms = round((time.perf_counter() - start) * 1000)
        audit.info("tool=%s CONFIRMADA ok=%s ms=%d args=%s result=%s", pending.tool.name, ok, ms,
                   json.dumps(pending.args, ensure_ascii=False), result[:200].replace("\n", " "))
        return ok, result
