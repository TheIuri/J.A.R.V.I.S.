import base64

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import config
from jarvis.config import LLMProviderConfig
from jarvis.llm import FallbackLLM, LLMError, OpenAICompatLLM
from jarvis.main import create_app
from jarvis.pipeline import Assistant
from jarvis.tts import _voice_url, clean_for_speech


class FakeSTT:
    name = "fake-stt"

    def __init__(self, text="hola jarvis"):
        self.text = text

    def transcribe(self, audio):
        return self.text


class FakeTTS:
    name = "fake-tts"

    def synthesize(self, text):
        return b"RIFFfake"


def llm_with(handler, name="fake"):
    cfg = LLMProviderConfig(name=name, base_url="http://llm", api_key="k", model="m")
    client = httpx.Client(base_url="http://llm", transport=httpx.MockTransport(handler))
    return OpenAICompatLLM(cfg, timeout_s=5, max_tokens=50, client=client)


def ok_handler(reply="Buenos dias."):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return handler


def fail_handler(request):
    return httpx.Response(429, json={"error": "rate limit"})


def make_assistant(stt=None, llm=None):
    llm = llm or FallbackLLM([llm_with(ok_handler())])
    return Assistant(stt or FakeSTT(), llm, FakeTTS(), "sistema", history_turns=2)


def test_fallback_uses_next_provider_on_error():
    llm = FallbackLLM([llm_with(fail_handler, "a"), llm_with(ok_handler("vale"), "b")])
    reply = llm.chat([{"role": "user", "content": "hola"}])
    assert reply.text == "vale"
    assert reply.provider == "b:m"


def test_fallback_raises_when_all_fail():
    llm = FallbackLLM([llm_with(fail_handler, "a"), llm_with(fail_handler, "b")])
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "hola"}])


def test_llm_error_includes_provider_reason():
    def not_found(request):
        return httpx.Response(404, json={"error": {"message": "The model `m` does not exist"}})

    with pytest.raises(LLMError, match="HTTP 404.*does not exist"):
        llm_with(not_found).chat([{"role": "user", "content": "hola"}])


def test_audio_turn_records_timings_and_history():
    assistant = make_assistant()
    result = assistant.handle_audio(b"wav")
    assert result.transcript == "hola jarvis"
    assert result.reply == "Buenos dias."
    assert result.audio == b"RIFFfake"
    assert {"stt", "llm", "tts", "total"} <= result.timings_ms.keys()
    assert len(assistant._history["default"]) == 2


def test_history_is_bounded_and_resettable():
    assistant = make_assistant()
    for _ in range(5):
        assistant.handle_text("hola")
    assert len(assistant._history["default"]) == 4
    assistant.reset()
    assert "default" not in assistant._history


def test_empty_transcript_skips_llm():
    def boom(request):
        raise AssertionError("no deberia llamar al LLM")

    assistant = make_assistant(stt=FakeSTT(""), llm=FallbackLLM([llm_with(boom)]))
    result = assistant.handle_audio(b"wav")
    assert result.reply == "" and result.audio is None


def test_api_requires_token():
    client = TestClient(create_app(make_assistant(), api_token="secreto"))
    assert client.post("/api/chat", json={"text": "hola"}).status_code == 401
    bad = client.post("/api/chat", json={"text": "hola"}, headers={"Authorization": "Bearer otro"})
    assert bad.status_code == 401


def test_api_voice_roundtrip():
    client = TestClient(create_app(make_assistant(), api_token="secreto"))
    resp = client.post(
        "/api/voice",
        files={"audio": ("a.wav", b"wav", "audio/wav")},
        headers={"Authorization": "Bearer secreto"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "Buenos dias."
    assert base64.b64decode(body["audio_wav_b64"]) == b"RIFFfake"


def test_api_returns_502_when_llm_down():
    client = TestClient(create_app(make_assistant(llm=FallbackLLM([llm_with(fail_handler)])), api_token="s"))
    resp = client.post("/api/chat", json={"text": "hola"}, headers={"Authorization": "Bearer s"})
    assert resp.status_code == 502


def test_settings_require_token_and_keys(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        config.load_settings()
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("LLM_PROVIDERS", "groq,ollama")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="groq"):
        config.load_settings()
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2:3b")
    settings = config.load_settings()
    assert [p.name for p in settings.llm_providers] == ["groq", "ollama"]
    assert settings.llm_providers[1].model == "llama3.2:3b"


def test_piper_voice_url_and_cleanup():
    assert _voice_url("es_ES-davefx-medium", ".onnx").endswith("es/es_ES/davefx/medium/es_ES-davefx-medium.onnx")
    assert clean_for_speech("**Hola**,  `señor`") == "Hola, señor"
