import httpx
import pytest

from jarvis import config
from jarvis.config import LLMProviderConfig
from jarvis.llm import FallbackLLM, LLMError, OpenAICompatLLM, _retry_after, usage_report

OK = {"choices": [{"message": {"content": "hola"}}], "usage": {"total_tokens": 42}}
GROQ_HEADERS = {
    "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "150",
    "x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "7000",
    "x-ratelimit-reset-requests": "1h2m", "x-ratelimit-reset-tokens": "7.5s",
}


def llm(name, handler, model="m"):
    cfg = LLMProviderConfig(name=name, base_url="http://llm", api_key="k", model=model)
    return OpenAICompatLLM(cfg, 5, 100, client=httpx.Client(base_url="http://llm", transport=httpx.MockTransport(handler)))


def limited(seconds="10.6s"):
    body = {"error": {"message": f"Rate limit reached ... Please try again in {seconds}. Need more tokens?"}}
    return httpx.Response(429, json=body)


def test_retry_after_is_read_from_every_provider_format():
    assert _retry_after(limited("10.6s")) == 10.6
    assert _retry_after(limited("1m2.5s")) == 62.5
    assert _retry_after(httpx.Response(429, text='[{"error": {"details": [{"retryDelay": "31s"}]}}]')) == 31
    assert _retry_after(httpx.Response(429, headers={"retry-after": "7"})) == 7
    assert _retry_after(httpx.Response(429, text="quota exceeded")) is None
    cerebras = '{"message":"Requests per minute limit exceeded - too many requests sent.","type":"too_many_requests_error"}'
    assert _retry_after(httpx.Response(429, text=cerebras, headers={
        "x-ratelimit-reset-requests-minute": "12.4", "x-ratelimit-reset-tokens-minute": "3",
        "x-ratelimit-reset-requests-day": "40000"})) == 12.4
    assert _retry_after(httpx.Response(429, text=cerebras)) == 30  # por minuto, sin plazo


def test_agents_wait_for_per_minute_limits_but_conversation_does_not():
    calls = []

    def handler(request):
        calls.append(1)
        return limited("10s") if len(calls) == 1 else httpx.Response(200, json=OK)

    slept = []
    chain = FallbackLLM([llm("groq", handler, "patient")], sleep=slept.append)
    assert chain.chat([{"role": "user", "content": "x"}], patient=True).text == "hola"
    assert slept == [11]
    calls.clear()
    with pytest.raises(LLMError, match="429"):
        FallbackLLM([llm("groq", handler, "impatient")], sleep=slept.append).chat([{"role": "user", "content": "x"}])
    assert slept == [11]  # la conversación no espera


def test_long_or_unknown_waits_are_not_worth_it():
    slept = []
    daily = FallbackLLM([llm("gemini", lambda r: httpx.Response(429, text="quota"), "daily")], sleep=slept.append)
    with pytest.raises(LLMError):
        daily.chat([{"role": "user", "content": "x"}], patient=True)
    hour = FallbackLLM([llm("groq", lambda r: limited("1h2m"), "hour")], sleep=slept.append)
    with pytest.raises(LLMError):
        hour.chat([{"role": "user", "content": "x"}], patient=True)
    assert slept == []


def test_usage_tracks_counts_headers_and_limits():
    ok = llm("groq", lambda r: httpx.Response(200, json=OK, headers=GROQ_HEADERS), "usage-a")
    ok.chat([{"role": "user", "content": "x"}])
    bad = llm("gemini", lambda r: limited("30s"), "usage-b")
    with pytest.raises(LLMError):
        bad.chat([{"role": "user", "content": "x"}])
    a, b = usage_report(["groq:usage-a", "gemini:usage-b"])
    assert a["requests"] == 1 and a["tokens"] == 42 and a["state"] == "low"  # 150 de 1000 peticiones
    meters = {m["name"]: m for m in a["meters"]}
    assert meters["requests"]["window"] == "día" and meters["requests"]["remaining"] == 150
    assert meters["tokens"]["window"] == "min" and meters["tokens"]["limit"] == 8000
    assert b["state"] == "limit" and 25 <= b["blocked_for"] <= 30 and b["limited"] == 1


def test_provider_specs(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "c")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GROQ_REASONING_EFFORT", "medium")
    groq = config._llm_provider("groq")
    assert (groq.model, groq.reasoning_effort) == ("openai/gpt-oss-120b", "medium")
    llama = config._llm_provider("groq:llama-3.3-70b-versatile")
    assert (llama.name, llama.model, llama.reasoning_effort) == ("groq", "llama-3.3-70b-versatile", "")
    cerebras = config._llm_provider("cerebras")
    assert cerebras.base_url == "https://api.cerebras.ai/v1" and cerebras.reasoning_effort == "low"
    assert config._llm_provider("openrouter:meta-llama/llama-3.3-70b-instruct:free").model == (
        "meta-llama/llama-3.3-70b-instruct:free")
    with pytest.raises(ValueError, match="desconocido"):
        config._llm_provider("chatgpt")


def test_usage_endpoint():
    from fastapi.testclient import TestClient

    from jarvis.main import create_app
    from tests.test_core import make_assistant

    assistant = make_assistant()
    client = TestClient(create_app(assistant, api_token="s"))
    assert client.get("/api/usage").status_code == 401
    assistant.handle_text("hola")
    out = client.get("/api/usage", headers={"Authorization": "Bearer s"}).json()
    assert out["chain"] == [p.name for p in assistant.llm.providers] and out["agents"] == []
    assert out["models"][0]["requests"] >= 1


def test_rotation_skips_models_at_their_limit():
    calls = []

    def first(request):
        calls.append("first")
        return limited("30s")

    def second(request):
        calls.append("second")
        return httpx.Response(200, json=OK)

    chain = FallbackLLM([llm("cerebras", first, "rot-a"), llm("groq", second, "rot-b")])
    assert chain.chat([{"role": "user", "content": "x"}]).provider == "groq:rot-b"
    assert calls == ["first", "second"]
    calls.clear()
    chain.chat([{"role": "user", "content": "x"}])
    assert calls == ["second"]  # en su limite: ni se intenta mientras dura la espera


def test_rotation_anticipates_the_per_minute_token_budget():
    calls = []
    headers = {"x-ratelimit-limit-tokens-minute": "60000", "x-ratelimit-remaining-tokens-minute": "500",
               "x-ratelimit-reset-tokens-minute": "40"}

    def first(request):
        calls.append("first")
        return httpx.Response(200, json=OK, headers=headers)

    def second(request):
        calls.append("second")
        return httpx.Response(200, json=OK)

    chain = FallbackLLM([llm("cerebras", first, "bud-a"), llm("groq", second, "bud-b")])
    chain.chat([{"role": "user", "content": "x"}])
    assert calls == ["first"]
    calls.clear()
    chain.chat([{"role": "user", "content": "x" * 4000}])  # ~1000 tokens + 100 de respuesta > 500
    assert calls == ["second"]
    calls.clear()
    chain.chat([{"role": "user", "content": "x"}])  # peticion pequena: cabe
    assert calls == ["first"]
