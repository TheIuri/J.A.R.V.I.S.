import io
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis_hud  # noqa: E402
from wake import RATE, EndpointConfig, Endpointer, to_wav  # noqa: E402

BLOCK = 1280


def blocks(seconds, amplitude):
    n = int(RATE * seconds) // BLOCK
    rng = np.random.default_rng(1)
    return [rng.normal(0, amplitude, BLOCK).astype(np.int16) for _ in range(n)]


def run(ep, frames):
    for f in frames:
        state = ep.feed(f)
        if state != "continue":
            return state
    return "continue"


def test_endpointer_ends_after_silence_following_speech():
    ep = Endpointer(noise_floor=0.002)
    assert run(ep, blocks(0.3, 30) + blocks(1.2, 3000)) == "continue"
    assert run(ep, blocks(1.0, 30)) == "done"
    w = wave.open(io.BytesIO(ep.wav()))
    assert w.getframerate() == RATE and w.getnchannels() == 1


def test_endpointer_cancels_when_nothing_is_said():
    ep = Endpointer(noise_floor=0.002, cfg=EndpointConfig(start_timeout_s=1.0))
    assert run(ep, blocks(1.2, 30)) == "cancel"


def test_endpointer_caps_long_utterances_and_adapts_to_noise():
    ep = Endpointer(noise_floor=0.002, cfg=EndpointConfig(max_s=2.0))
    assert run(ep, blocks(3, 3000)) == "done"
    noisy = Endpointer(noise_floor=0.05)
    assert abs(noisy.threshold - 0.15) < 1e-9  # 3x el ruido de fondo
    assert len(to_wav(np.zeros(160, np.int16))) == 44 + 320


class FakeHud(jarvis_hud.Hud):
    def __init__(self):  # sin servidor ni acciones
        self._events = []
        self._cond = threading.Condition()
        self.actions = None
        self.wake = None


def test_events_long_poll_returns_as_soon_as_something_happens():
    hud = FakeHud()
    threading.Timer(0.1, lambda: hud.push({"type": "wake"})).start()
    start = time.monotonic()
    assert hud.take_events(5) == [{"type": "wake"}]
    assert time.monotonic() - start < 2
    assert hud.take_events(0.05) == []


def test_busy_pauses_wake_detection():
    hud = FakeHud()
    hud.set_busy(True)  # sin "Hey Jarvis" no hace nada
    hud.wake = type("W", (), {"paused": threading.Event()})()
    hud.set_busy(True)
    assert hud.wake.paused.is_set()
    hud.set_busy(False)
    assert not hud.wake.paused.is_set()


def test_pc_wol_action_validates_mac():
    from pc_actions import PCActions

    pc = PCActions({}, announce=lambda t: None)
    assert pc.run({"action": "wol", "mac": "no-es-una-mac"}) == "MAC no valida"
    assert pc.run({"action": "wol", "mac": "AA:BB:CC:DD:EE:FF", "device": "sobremesa"}) == "encendido enviado a sobremesa"


def test_delegate_runs_claude_code_safely(tmp_path):
    import os
    import stat
    import time as t

    from delegate import ALLOWED, Delegate

    # Un "claude" falso que guarda sus argumentos y lo que recibe por la entrada estándar.
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/args"\n'
        f'cat > "{tmp_path}/stdin"\n'
        'echo "RESUMEN: Hecho."\necho "# Informe"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    reports, spoken = [], []
    d = Delegate(lambda title, text: reports.append((title, text)), spoken.append, exe=str(fake))
    task = 'compara placas solares"; rm -rf / #'
    assert d.start(task) == "tarea enviada a Claude"
    for _ in range(50):
        if reports:
            break
        t.sleep(0.1)
    assert reports == [(task, "RESUMEN: Hecho.\n# Informe")] and spoken == []
    args = (tmp_path / "args").read_text().split("\n")
    assert task not in "\n".join(args)  # la tarea nunca va en la línea de comandos
    assert args[args.index("--allowedTools") + 1] == ALLOWED and "--strict-mcp-config" in args
    assert "Read" in args[args.index("--disallowedTools") + 1]
    assert task in (tmp_path / "stdin").read_text()
    assert os.path.exists(fake)


def test_delegate_without_claude_installed():
    from delegate import Delegate
    from pc_actions import PCActions

    d = Delegate(lambda *a: None, lambda t: None, exe=None)
    d.available = lambda: None
    assert PCActions({}, lambda t: None, d).run({"action": "delegate", "task": "x"}) == "Claude Code no está instalado en este PC"
    assert PCActions({}, lambda t: None).run({"action": "delegate", "task": "x"}).startswith("delegar tareas")
