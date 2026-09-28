import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import config
from jarvis.llm import FallbackLLM
from jarvis.main import create_app
from jarvis.pipeline import Assistant
from jarvis.tools import ToolContext
from jarvis.tools.homeassistant import HomeAssistant, ha_tools
from jarvis.tools.vision import Vision, camera_tool, check_image
from tests.test_core import llm_with
from tests.test_tools import NoTTS, ScriptedLLM, registry

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 100).decode()


def vision_llm(seen):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Veo una taza roja."}}]})

    return FallbackLLM([llm_with(handler, "gemini")])


def test_check_image():
    assert check_image(JPEG) == JPEG and check_image(None) is None
    with pytest.raises(ValueError, match="JPEG o PNG"):
        check_image(base64.b64encode(b"GIF89a....").decode())
    with pytest.raises(ValueError, match="no válida"):
        check_image("no es base64!!")


def test_camera_tool_only_with_a_photo_and_sends_it_to_the_vision_model():
    seen = []
    tool = camera_tool(Vision(vision_llm(seen)))
    reg = registry(tool)
    assert reg.specs(ToolContext()) == []
    assert reg.specs(ToolContext(image=JPEG))[0]["function"]["name"] == "camera_look"
    assert reg.execute("camera_look", '{"question": "¿qué es esto?"}', ToolContext(image=JPEG)) == "Veo una taza roja."
    content = seen[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "¿qué es esto?"}
    assert content[1]["image_url"]["url"] == f"data:image/jpeg;base64,{JPEG}"
    assert "reasoning_effort" not in seen[0]


def test_vision_providers_config(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("LLM_PROVIDERS", "groq,gemini")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    for var in ("VISION_PROVIDERS", "AGENT_LLM_PROVIDERS", "GROQ_VISION_MODEL", "GEMINI_VISION_MODEL"):
        monkeypatch.delenv(var, raising=False)
    vision = config.load_settings().vision_providers
    assert [(p.name, p.model, p.reasoning_effort) for p in vision] == [
        ("gemini", "gemini-flash-latest", ""), ("groq", "meta-llama/llama-4-scout-17b-16e-instruct", "")]
    monkeypatch.setenv("VISION_PROVIDERS", "groq")
    monkeypatch.setenv("GROQ_VISION_MODEL", "otro-modelo")
    assert [(p.name, p.model) for p in config.load_settings().vision_providers] == [("groq", "otro-modelo")]


def test_home_assistant_camera():
    states = [{"entity_id": "camera.puerta", "state": "idle", "attributes": {"friendly_name": "Cámara puerta"}}]

    def handler(request):
        if request.url.path == "/api/states":
            return httpx.Response(200, json=states)
        assert request.url.path == "/api/camera_proxy/camera.puerta"
        return httpx.Response(200, content=b"\xff\xd8jpeg")

    ha = HomeAssistant("http://ha", "t", client=httpx.Client(transport=httpx.MockTransport(handler)))
    seen = []
    cam = ha_tools(ha, Vision(vision_llm(seen)))[0]
    assert cam.name == "home_camera"
    assert cam.fn(ToolContext(), camera="puerta", question="¿hay alguien?") == "Cámara puerta: Veo una taza roja."
    assert [t.name for t in ha_tools(ha)] == ["home_status", "home_control"]  # sin visión, sin cámaras


def test_api_passes_the_photo_to_the_turn():
    seen = []
    script = ScriptedLLM([[("camera_look", {"question": "¿qué ves?"})], "Una taza roja."])
    assistant = Assistant(None, script.llm(), NoTTS(), "s", tools=registry(camera_tool(Vision(vision_llm(seen)))))
    client = TestClient(create_app(assistant, api_token="s"))
    auth = {"Authorization": "Bearer s"}
    resp = client.post("/api/chat/stream", json={"text": "¿qué ves?", "image": JPEG}, headers=auth)
    assert json.loads(resp.text.splitlines()[-1])["tools_used"] == ["camera_look"] and seen
    assert "camera_look" in json.dumps(script.requests[0]["tools"])
    bad = client.post("/api/chat/stream", json={"text": "x", "image": "AAAA"}, headers=auth)
    assert bad.status_code == 400
