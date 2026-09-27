import httpx
import pytest

from jarvis.tools import ToolContext
from jarvis.tools.info import (
    Currency, News, WebSearch, Wikipedia, convert_tool, convert_units, news_tool, web_search_tool, wikipedia_tool,
)
from jarvis.tools.registry import ToolError

CTX = ToolContext()

DUCK_HTML = """
<div class="result results_links results_links_deep result--ad">
  <a rel="nofollow" class="result__a" href="https://ads.example/x">Anuncio</a>
  <a class="result__snippet" href="#">compra ya</a>
</div>
<div class="result results_links results_links_deep web-result ">
  <h2 class="result__title"><a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.fcbarcelona.es%2Fes%2F&amp;rut=abc">FC <b>Barcelona</b> &amp; web oficial</a></h2>
  <a class="result__snippet" href="#">El <b>Barça</b> ganó 3-1 al Madrid.</a>
</div>
<div class="result results_links results_links_deep web-result ">
  <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://es.wikipedia.org/wiki/FCB">FCB - Wikipedia</a></h2>
  <a class="result__snippet" href="#">Club de fútbol.</a>
</div>
"""


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_duckduckgo_results_skip_ads_and_unwrap_links():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        return httpx.Response(200, text=DUCK_HTML)

    out = web_search_tool(WebSearch(client=client(handler))).fn(CTX, query="resultado Barça")
    assert "q=resultado+Bar" in seen["body"] and "kl=es-es" in seen["body"]
    assert out.splitlines()[0] == "1. FC Barcelona & web oficial — El Barça ganó 3-1 al Madrid. (https://www.fcbarcelona.es/es/)"
    assert "Anuncio" not in out and out.count("\n") == 1


def test_brave_is_used_when_key_is_set():
    def handler(request):
        assert request.headers["X-Subscription-Token"] == "k"
        return httpx.Response(200, json={"web": {"results": [
            {"title": "T", "url": "https://t.es", "description": "D", "thumbnail": {"src": "https://img.es/t.jpg"}}]}})

    assert WebSearch("k", client(handler)).search("x") == [
        {"title": "T", "url": "https://t.es", "snippet": "D", "image": "https://img.es/t.jpg"}]


def test_search_errors_become_tool_errors():
    def down(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(ToolError, match="no responde"):
        WebSearch(client=client(down)).search("x")


def wiki_page(**page):
    return httpx.Response(200, json={"batchcomplete": True, "query": {"pages": [page]}})


def test_wikipedia_summary_and_disambiguation():
    def handler(request):
        assert request.url.path == "/w/api.php" and request.url.params["gsrsearch"] == "torre eiffel"
        return wiki_page(title="Torre Eiffel", extract="La torre Eiffel mide 330 m.", fullurl="https://es.wikipedia.org/wiki/T")

    assert wikipedia_tool(Wikipedia(client=client(handler))).fn(CTX, topic="torre eiffel") == "Torre Eiffel: La torre Eiffel mide 330 m."

    def ambiguous(request):
        return wiki_page(title="Mercurio", extract="Puede referirse a...", pageprops={"disambiguation": ""})

    assert "desambiguación" in wikipedia_tool(Wikipedia(client=client(ambiguous))).fn(CTX, topic="mercurio")

    def empty(request):
        return httpx.Response(200, json={"batchcomplete": True})

    with pytest.raises(ToolError, match="ningún artículo"):
        Wikipedia(client=client(empty)).summary("zzzz")


RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
<item><title>El Barça gana el clásico - El País</title><pubDate>Sat, 27 Sep 2026 18:00:00 GMT</pubDate><source url="https://elpais.com">El País</source></item>
<item><title>Sube el precio de la luz - RTVE</title><pubDate>Sat, 27 Sep 2026 09:30:00 GMT</pubDate><source url="https://rtve.es">RTVE</source></item>
</channel></rss>"""


def test_news_headlines_with_and_without_topic():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, text=RSS)

    tool = news_tool(News(client=client(handler)))
    assert tool.fn(CTX).splitlines() == [
        "- El Barça gana el clásico (El País, 27/09 20:00)",  # hora de Madrid
        "- Sube el precio de la luz (RTVE, 27/09 11:30)",
    ]
    tool.fn(CTX, topic="Barça", max_results=1)
    assert urls[0].startswith("https://news.google.com/rss?") and "/rss/search?" in urls[1] and "q=Bar" in urls[1]


def test_unit_conversions():
    assert convert_units(10, "km", "millas") == pytest.approx(6.2137, rel=1e-4)
    assert convert_units(100, "°F", "celsius") == pytest.approx(37.7778, rel=1e-4)
    assert convert_units(2, "Kilómetros", "metros") == 2000
    assert convert_units(1, "gib", "mb") == pytest.approx(1073.741824)
    with pytest.raises(ToolError, match="no se puede convertir longitud en masa"):
        convert_units(1, "km", "kg")
    with pytest.raises(ToolError, match="no conozco"):
        convert_units(1, "furlongs", "km")
    tool = convert_tool(Currency(client(lambda r: httpx.Response(500))))
    assert tool.fn(CTX, value=1500, from_unit="m", to_unit="km") == "1.500 m = 1,5 km"


def test_currency_uses_ecb_rates():
    def handler(request):
        assert request.url.params["base"] == "USD" and request.url.params["symbols"] == "EUR"
        return httpx.Response(200, json={"base": "USD", "date": "2026-09-26", "rates": {"EUR": 0.9}})

    out = convert_tool(Currency(client(handler))).fn(CTX, value=50, from_unit="usd", to_unit="eur")
    assert out == "50 USD = 45 EUR (cambio del BCE del 2026-09-26)"
    with pytest.raises(ToolError, match="no soportada"):
        Currency(client(lambda r: httpx.Response(404))).convert(1, "EUR", "XXX")


def test_tools_fill_cards_for_the_hud():
    ctx = ToolContext()
    web_search_tool(WebSearch(client=client(lambda r: httpx.Response(200, text=DUCK_HTML)))).fn(ctx, query="barça")
    assert ctx.cards[0] == {
        "kind": "web", "title": "FC Barcelona & web oficial", "url": "https://www.fcbarcelona.es/es/",
        "text": "El Barça ganó 3-1 al Madrid.", "source": "fcbarcelona.es", "image": "",
        "icon": "https://icons.duckduckgo.com/ip3/fcbarcelona.es.ico",
    }

    def wiki(request):
        return wiki_page(title="Torre Eiffel", extract="Mide 330 m.", fullurl="https://es.wikipedia.org/wiki/Torre_Eiffel",
                         thumbnail={"source": "https://upload.wikimedia.org/eiffel.jpg"})

    ctx = ToolContext()
    wikipedia_tool(Wikipedia(client=client(wiki))).fn(ctx, topic="torre eiffel")
    assert ctx.cards[0]["image"] == "https://upload.wikimedia.org/eiffel.jpg" and ctx.cards[0]["source"] == "Wikipedia"

    ctx = ToolContext()
    news_tool(News(client=client(lambda r: httpx.Response(200, text=RSS)))).fn(ctx)
    assert [c["source"] for c in ctx.cards] == ["El País", "RTVE"] and ctx.cards[0]["kind"] == "news"


def test_cards_only_allow_web_links_and_https_images():
    from jarvis.tools.info import add_card

    ctx = ToolContext()
    add_card(ctx, "web", "x", "javascript:alert(1)", image="http://inseguro/img.jpg")
    assert ctx.cards[0]["url"] == "" and ctx.cards[0]["image"] == ""
