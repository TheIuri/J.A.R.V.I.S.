"""Vigilar precios: "avísame si el PETG baja de 6 €".

Guarda una vigilancia (un encargo para un agente, cada cuántas horas y el precio objetivo). El
vigilante la relanza sola; cada informe que vuelve deja su precio en el histórico y, si baja, JARVIS
avisa por voz y al móvil. El histórico se ve en el HUD como una gráfica.
"""

from __future__ import annotations

from ..prices import MAX_WATCHES, MIN_HOURS, PriceStore, euros, parse_price, prices_card, slug
from .registry import Tool, ToolContext, ToolError

AGENTS = ("compras", "investigador")


def price_tools(store: PriceStore, agents=lambda: AGENTS) -> list[Tool]:
    def watch(ctx: ToolContext, action: str, task: str = "", product: str = "", target: str = "",
              every_hours: int = 24, watch_id: int | None = None, agent: str = "compras") -> str:
        if action == "crear":
            if not task:
                raise ToolError("dime qué hay que vigilar, p. ej. 'precio del filamento PETG de 1 kg'")
            available = list(agents())
            if agent not in available:
                agent = available[0] if available else "compras"
            cents = parse_price(target) if target else 0
            if target and cents is None:
                raise ToolError(f"no entiendo el precio objetivo '{target}'; dilo como '6,50 €'")
            try:
                w = store.add_watch(agent, task, product or task, cents or 0, every_hours)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            goal = f" y te aviso si baja de {euros(w.target)}" if w.target else " y te aviso si baja de precio"
            return (f"Vigilancia {w.id} creada: lo miro cada {w.every_hours} h{goal}. "
                    f"Quedan {MAX_WATCHES - len(store.watches())} huecos.")
        if action == "quitar":
            if watch_id is None:
                raise ToolError("dime el id de la vigilancia (míralo con action=listar)")
            try:
                return f"Vigilancia quitada: {store.remove_watch(watch_id).task}"
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
        watches = store.watches()
        products = store.products()
        if products:
            ctx.cards.append(prices_card(store, products, title="Precios vigilados"))
        if not watches:
            return "No hay ninguna vigilancia de precios."
        return "\n".join(w.line() for w in watches)

    def history(ctx: ToolContext, product: str) -> str:
        points = store.history(product)
        if not points:
            known = ", ".join(p["product"] for p in store.products()[:8])
            raise ToolError(f"no tengo precios de '{product}'" + (f"; tengo de: {known}" if known else ""))
        match = [p for p in store.products() if slug(p["product"]) == slug(points[-1]["product"])]
        ctx.cards.append(prices_card(store, match or [{"product": points[-1]["product"], "slug": slug(product),
                                                       "price": points[-1]["price"], "cents": points[-1]["cents"]}],
                                     title=f"Precio de {points[-1]['product']}"))
        low = min(points, key=lambda p: p["cents"])
        trend = "igual que la última vez"
        if len(points) > 1:
            before = points[-2]["cents"]
            now = points[-1]["cents"]
            if now != before:
                trend = f"{'baja' if now < before else 'sube'} desde {points[-2]['price']}"
        return (f"{points[-1]['product']}: ahora {points[-1]['price']} ({trend}). Lo más barato visto, "
                f"{low['price']} el {low['date']}. {len(points)} días con datos.")

    return [
        Tool(
            name="price_watch",
            description=(
                "Vigila el precio de algo cada cierto tiempo y avisa cuando baja ('avísame si el PETG baja de 6 €'). "
                "action=crear (task: qué mirar; target: precio objetivo opcional), listar o quitar (watch_id). "
                f"Mínimo cada {MIN_HOURS} h. Lanza un agente cada vez, así que no pongas muchas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["crear", "listar", "quitar"]},
                    "task": {"type": "string", "description": "Qué buscar y mirar, con detalle"},
                    "product": {"type": "string", "description": "Nombre corto del producto, para el histórico"},
                    "target": {"type": "string", "description": "Precio objetivo, p. ej. '6,50 €'"},
                    "every_hours": {"type": "integer", "minimum": MIN_HOURS, "maximum": 336},
                    "watch_id": {"type": "integer", "minimum": 1},
                    "agent": {"type": "string", "enum": list(AGENTS)},
                },
                "required": ["action"],
            },
            fn=watch,
        ),
        Tool(
            name="price_history",
            description="Cómo ha ido el precio de algo que ya se ha mirado antes (gráfica y el más barato visto).",
            parameters={
                "type": "object",
                "properties": {"product": {"type": "string", "description": "Nombre del producto"}},
                "required": ["product"],
            },
            fn=history,
        ),
    ]
