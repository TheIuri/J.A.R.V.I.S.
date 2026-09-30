"""Histórico de precios: leer precios de un informe, guardarlos y avisar cuando bajan."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.prices import PriceStore, euros, from_report, parse_price, prices_card, slug
from jarvis.tools.prices import price_tools
from jarvis.tools.registry import ToolContext, ToolError

TZ = ZoneInfo("Europe/Madrid")

REPORT = """# Filamentos
| Material | Precio mínimo | Marca | Enlace |
|---|---|---|---|
| PLA Basic | 6,50 € | Filamentor | https://filamentor.es/pla |
| PETG | 8,95 € | Filamentor | https://filamentor.es/petg |
| Resina | no visto | - | https://filamentor.es/resina |

Los precios cambian.
"""


@pytest.fixture()
def store(tmp_path):
    s = PriceStore(tmp_path / "p.db", "Europe/Madrid")
    yield s
    s.close()


@pytest.mark.parametrize(
    "text,cents",
    [
        ("6,50 €", 650), ("€6.50", 650), ("1.299,00 €", 129900), ("desde 19,99 €", 1999), ("$12.30", 1230),
        ("29 €", 2900), ("no visto", None), ("", None), ("16 GB", None), ("2024", None), ("—", None),
        ("1.250.000 €", 125000000 if False else None),  # por encima del máximo razonable: no es un precio
    ],
)
def test_parse_price(text, cents):
    assert parse_price(text) == cents


def test_euros_formato_espanol():
    assert euros(129900) == "1.299,00 €"
    assert euros(650) == "6,50 €"


def test_from_report_lee_la_tabla():
    items = from_report(REPORT)
    assert [(i["name"], i["cents"]) for i in items] == [("PLA Basic", 650), ("PETG", 895)]
    assert items[0]["url"] == "https://filamentor.es/pla"


def test_from_report_prefiere_las_tarjetas():
    cards = [{"title": "PLA Basic", "price": "5,99 €", "url": "https://tienda.es/pla"}]
    items = from_report(REPORT, cards)
    pla = next(i for i in items if slug(i["name"]) == slug("PLA Basic"))
    assert pla["cents"] == 599 and pla["url"] == "https://tienda.es/pla"


def test_historico_y_bajada(store):
    assert store.record_report(REPORT, agent="compras") == []  # la primera vez no hay con qué comparar
    barato = REPORT.replace("6,50 €", "5,50 €")
    drops = store.record_report(barato, agent="compras")
    assert [d.product for d in drops] == ["PLA Basic"]
    assert drops[0].cents == 550 and drops[0].before == 650 and round(drops[0].pct) == 15
    assert "5,50 €" in drops[0].text()
    historia = store.history("PLA Basic")
    assert historia[-1]["cents"] == 550


def test_subida_no_avisa(store):
    store.record_report(REPORT)
    assert store.record_report(REPORT.replace("6,50 €", "9,50 €")) == []


def test_bajada_minima_no_avisa(store):
    store.record_report(REPORT)
    assert store.record_report(REPORT.replace("6,50 €", "6,45 €")) == []  # menos del 3%


def test_objetivo_avisa_aunque_baje_poco(store):
    store.record_report(REPORT)
    store.add_watch("compras", "precio del PLA", product="PLA Basic", target_cents=645)
    drops = store.record_report(REPORT.replace("6,50 €", "6,40 €"))
    assert [d.product for d in drops] == ["PLA Basic"]
    assert drops[0].target == 645 and "objetivo" in drops[0].text()


def test_historico_un_punto_por_dia(store):
    dia = datetime(2026, 9, 1, 10, 0, tzinfo=TZ)
    store.record([{"name": "PLA", "cents": 700, "url": ""}], now=dia)
    store.record([{"name": "PLA", "cents": 650, "url": ""}], now=dia.replace(hour=20))
    store.record([{"name": "PLA", "cents": 800, "url": ""}], now=dia + timedelta(days=1))
    puntos = store.history("PLA", days=3650)
    assert [(p["date"], p["cents"]) for p in puntos] == [("2026-09-01", 650), ("2026-09-02", 800)]


def test_vigilancias(store):
    w = store.add_watch("compras", "precio del PETG de 1 kg", product="PETG", target_cents=600, every_hours=2)
    assert w.every_hours == 6  # se sube al mínimo
    assert [x.id for x in store.due()] == [w.id]
    assert store.due() == []  # ya se lanzó: no toca otra vez
    assert store.due(datetime.now(TZ) + timedelta(hours=7)) != []
    assert store.remove_watch(w.id).task == "precio del PETG de 1 kg"
    assert store.watches() == []


def test_limite_de_vigilancias(store):
    for i in range(12):
        store.add_watch("compras", f"cosa {i}")
    with pytest.raises(ValueError):
        store.add_watch("compras", "una más")


def test_tarjeta_para_el_hud(store):
    store.record_report(REPORT)
    store.record_report(REPORT.replace("6,50 €", "5,50 €"))
    card = prices_card(store, store.products(), store.record_report(REPORT.replace("6,50 €", "4,50 €")))
    assert card["kind"] == "prices"
    pla = next(i for i in card["items"] if i["product"] == "PLA Basic")
    assert len(pla["series"]) >= 1 and pla["url"] == "https://filamentor.es/pla"
    assert card["drops"][0]["product"] == "PLA Basic"


def test_tools(store):
    tools = {t.name: t for t in price_tools(store)}
    ctx = ToolContext()
    assert "no hay" in tools["price_watch"].fn(ctx, action="listar").lower()
    out = tools["price_watch"].fn(ctx, action="crear", task="precio del PETG", target="6,50 €", every_hours=12)
    assert "6,50 €" in out
    assert "PETG" in tools["price_watch"].fn(ctx, action="listar")
    store.record_report(REPORT)
    ctx2 = ToolContext()
    texto = tools["price_history"].fn(ctx2, product="PETG")
    assert "8,95 €" in texto and ctx2.cards[0]["kind"] == "prices"
    with pytest.raises(ToolError):
        tools["price_history"].fn(ctx2, product="una cosa que no existe")
    with pytest.raises(ToolError):
        tools["price_watch"].fn(ctx, action="crear", task="algo", target="barato")
