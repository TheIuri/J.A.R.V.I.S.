import base64
import json

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


def llm_with(handler, name="fake", reasoning_effort=""):
    cfg = LLMProviderConfig(name=name, base_url="http://llm", api_key="k", model="m", reasoning_effort=reasoning_effort)
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


def test_reasoning_effort_is_sent_only_when_set():
    bodies = []

    def capture(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "vale"}}]})

    llm_with(capture, reasoning_effort="low").chat([{"role": "user", "content": "hola"}])
    llm_with(capture).chat([{"role": "user", "content": "hola"}])
    assert bodies[0]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in bodies[1]


def test_empty_reply_is_an_error_so_fallback_can_act():
    llm = FallbackLLM([llm_with(ok_handler(""), "a"), llm_with(ok_handler("vale"), "b")])
    assert llm.chat([{"role": "user", "content": "hola"}]).provider == "b:m"


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
    assert settings.llm_providers[0].reasoning_effort == "low"
    assert settings.llm_providers[1].reasoning_effort == ""


def test_piper_voice_url_and_cleanup():
    assert _voice_url("es_ES-davefx-medium", ".onnx").endswith("es/es_ES/davefx/medium/es_ES-davefx-medium.onnx")
    assert clean_for_speech("**Hola**,  `señor`") == "Hola, señor"


def test_hud_is_served_without_token_but_api_still_needs_it():
    client = TestClient(create_app(make_assistant(), api_token="secreto"))
    assert client.get("/", follow_redirects=False).headers["location"] == "/hud/"
    page = client.get("/hud/")
    assert page.status_code == 200 and "J.A.R.V.I.S." in page.text
    js = client.get("/hud/hud.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    assert client.get("/hud/config").json() == {"mode": "server", "pc_apps": None}
    assert client.get("/hud/../jarvis/config.py").status_code == 404
    assert client.post("/api/chat", json={"text": "hola"}).status_code == 401


def test_agent_can_use_its_own_llm_chain(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("LLM_PROVIDERS", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.delenv("AGENT_LLM_PROVIDERS", raising=False)
    assert [p.name for p in config.load_settings().agent_llm_providers] == ["groq"]
    monkeypatch.setenv("AGENT_LLM_PROVIDERS", "gemini,groq")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="gemini"):
        config.load_settings()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    settings = config.load_settings()
    assert [p.name for p in settings.agent_llm_providers] == ["gemini", "groq"]
    assert [p.name for p in settings.llm_providers] == ["groq"]


def test_hud_can_choose_the_provider_and_others_stay_as_fallback():
    llm = FallbackLLM([llm_with(ok_handler("soy groq"), "groq"), llm_with(ok_handler("soy gemini"), "gemini")])
    assert [(m["id"], m["provider"], m["chain"]) for m in llm.models()] == [("groq:m", "groq", True), ("gemini:m", "gemini", True)]
    assert llm.chat([{"role": "user", "content": "hola"}]).text == "soy groq"
    assert llm.chat([{"role": "user", "content": "hola"}], prefer="gemini").text == "soy gemini"
    broken = FallbackLLM([llm_with(ok_handler("soy groq"), "groq"), llm_with(fail_handler, "gemini")])
    assert broken.chat([{"role": "user", "content": "hola"}], prefer="gemini").text == "soy groq"
    # El 429 de "broken" deja a gemini:m (el mismo modelo) apartado un rato; aqui se prueba otra cosa.
    for p in llm.providers:
        p.usage.blocked_until = 0

    assistant = Assistant(FakeSTT(), llm, FakeTTS(), "s")
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    assert [m["id"] for m in client.get("/api/models", headers=auth).json()["models"]][:2] == ["groq:m", "gemini:m"]
    resp = client.post("/api/chat/stream", json={"text": "hola", "model": "gemini"}, headers=auth)
    assert json.loads(resp.text.splitlines()[-1])["reply"] == "soy gemini"
    text = client.post("/api/transcribe", files={"audio": ("a.wav", b"wav", "audio/wav")}, headers=auth).json()
    assert text == {"text": "hola jarvis"}
    assert client.post("/api/transcribe", files={"audio": ("a.wav", b"wav", "audio/wav")}).status_code == 401


def _wav(seconds=0.2, rate=24000):
    import io
    import wave as w

    buf = io.BytesIO()
    with w.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\x00\x10" * int(rate * seconds))
    return buf.getvalue()


def test_edge_tts_converts_to_wav_and_falls_back_to_piper(monkeypatch):
    import io
    import wave as w

    import edge_tts

    from jarvis.tts import EdgeTTS

    seen = {}

    class FakeCommunicate:
        def __init__(self, text, voice, rate, pitch):
            seen.update(text=text, voice=voice, rate=rate, pitch=pitch)

        async def stream(self):
            data = _wav()
            yield {"type": "WordBoundary"}
            yield {"type": "audio", "data": data[:1000]}
            yield {"type": "audio", "data": data[1000:]}

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    tts = EdgeTTS("es-ES-ElviraNeural", "+10%", "-2Hz", fallback=FakeTTS())
    out = tts.synthesize("**Hola**, señor")
    assert seen == {"text": "Hola, señor", "voice": "es-ES-ElviraNeural", "rate": "+10%", "pitch": "-2Hz"}
    with w.open(io.BytesIO(out)) as f:
        assert f.getnchannels() == 1 and f.getframerate() == 24000 and f.getnframes() > 4000

    class Broken(FakeCommunicate):
        async def stream(self):
            raise ConnectionError("sin internet")
            yield  # pragma: no cover

    monkeypatch.setattr(edge_tts, "Communicate", Broken)
    assert tts.synthesize("hola") == b"RIFFfake"  # habla el respaldo (Piper)
    assert tts.name == "edge:es-ES-ElviraNeural (respaldo fake-tts)"
    with pytest.raises(ValueError, match="EDGE_RATE"):
        EdgeTTS(rate="rapido")
