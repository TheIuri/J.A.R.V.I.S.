"""Acciones que JARVIS puede ejecutar en este PC (Nivel 2).

Doble control: el servidor solo propone acciones de una lista cerrada y este modulo
vuelve a comprobarlas. Nada de ejecutar comandos arbitrarios.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

IS_WINDOWS = sys.platform == "win32"

# Teclas virtuales multimedia de Windows.
VK = {
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
}


def load_apps(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k.lower(): v for k, v in data.items() if not k.startswith("_")}


def _press(key: str, times: int = 1) -> None:
    if not IS_WINDOWS:
        raise RuntimeError("solo disponible en Windows")
    import ctypes

    for _ in range(times):
        ctypes.windll.user32.keybd_event(VK[key], 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK[key], 0, 2, 0)  # KEYEVENTF_KEYUP


def _set_volume(level: int) -> None:
    try:
        from pycaw.pycaw import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()
        volume = getattr(speakers, "EndpointVolume", None)
        if volume is None:  # versiones antiguas de pycaw
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume

            volume = cast(speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(level / 100, None)
    except ImportError:
        # Sin pycaw: bajar del todo y subir a pasos de 2%.
        _press("volume_down", 50)
        _press("volume_up", round(level / 2))


class PCActions:
    def __init__(self, apps: dict[str, str], announce: Callable[[str], None], delegate=None):
        self.apps = apps
        self.announce = announce  # dice un texto en voz alta (para los temporizadores)
        self.delegate = delegate  # delegate.Delegate: tareas para Claude Code (membresia)
        self.timers: list[threading.Timer] = []

    def run(self, action: dict) -> str:
        kind = action.get("action")
        try:
            if kind == "open_app":
                target = self.apps.get(str(action.get("app", "")).lower())
                if target is None:
                    return f"app no permitida: {action.get('app')}"
                if target.startswith(("http://", "https://")):
                    webbrowser.open(target)
                elif IS_WINDOWS:
                    os.startfile(target)  # noqa: S606 - destino de la lista permitida
                else:
                    return "abrir apps solo esta soportado en Windows"
                return f"abierto {action['app']}"
            if kind == "open_url":
                url = str(action.get("url", ""))
                if urlparse(url).scheme not in ("http", "https"):
                    return "URL rechazada"
                webbrowser.open(url)
                return f"abierta {url}"
            if kind == "volume":
                mode = action.get("mode")
                if mode == "set":
                    _set_volume(max(0, min(100, int(action.get("level") or 0))))
                elif mode in ("up", "down"):
                    _press(f"volume_{mode}", 5)  # ~10%
                elif mode == "mute":
                    _press("mute")
                else:
                    return f"modo de volumen desconocido: {mode}"
                return f"volumen {mode}"
            if kind == "media":
                key = action.get("key")
                if key not in ("play_pause", "next", "previous"):
                    return f"tecla multimedia desconocida: {key}"
                _press(key)
                return f"multimedia {key}"
            if kind == "timer":
                seconds = int(action.get("seconds", 0))
                label = str(action.get("label") or "").strip()
                if not 0 < seconds <= 86400:
                    return "duracion de temporizador invalida"
                message = f"Señor, el temporizador {('de ' + label) if label else ''} ha terminado.".replace("  ", " ")
                timer = threading.Timer(seconds, self.announce, args=(message,))
                timer.daemon = True
                timer.start()
                self.timers.append(timer)
                return f"temporizador de {seconds}s"
            if kind == "wol":
                mac = str(action.get("mac", ""))
                if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", mac):
                    return "MAC no valida"
                packet = b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    for port in (9, 7):
                        sock.sendto(packet, ("255.255.255.255", port))
                return f"encendido enviado a {action.get('device', mac)}"
            if kind == "delegate":
                if self.delegate is None:
                    return "delegar tareas solo funciona con el HUD del PC"
                return self.delegate.start(str(action.get("task", ""))[:1500])
            return f"accion desconocida: {kind}"
        except Exception as exc:  # una accion fallida no debe cerrar el cliente
            return f"error en {kind}: {exc}"

    def cancel_all(self) -> None:
        for t in self.timers:
            t.cancel()
        self.timers.clear()
