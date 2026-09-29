"""Todo lo que pide el HUD al servidor tiene que pasar por el proxy del HUD del PC (si no, en el PC no aparece)."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis_hud  # noqa: E402

HUD_JS = Path(__file__).resolve().parents[2] / "server" / "jarvis" / "web" / "hud.js"


def test_every_api_path_the_hud_uses_is_proxied_on_the_pc():
    js = HUD_JS.read_text(encoding="utf-8")
    fixed = set(re.findall(r'api\("(/api/[^"?]+)', js))
    templated = set(re.findall(r"api\(`(/api/[^`$?]+)", js))
    streamed = {f"{p}/stream" for p in re.findall(r'ask\("(/api/[^"]+)"', js)}
    allowed = jarvis_hud.PROXY_GET | jarvis_hud.PROXY_POST | jarvis_hud.STREAM_POST
    missing = sorted(p for p in fixed | streamed if p not in allowed)
    assert not missing, f"el HUD del PC no deja pasar: {missing}"
    # Rutas con una parte variable: /api/memories/<id>, /api/agents/custom/<clave>, /api/activity?...
    assert jarvis_hud.MEMORY_DELETE.match("/api/memories/12")
    assert jarvis_hud.CUSTOM_AGENT_DELETE.match("/api/agents/custom/a_vigilante_de_filamento")
    assert not jarvis_hud.CUSTOM_AGENT_DELETE.match("/api/agents/custom/../memories")
    for prefix in templated:
        assert prefix.rstrip("/") in allowed or prefix in ("/api/memories/", "/api/agents/custom/"), prefix
