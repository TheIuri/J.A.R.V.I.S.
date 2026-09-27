"""Wake-on-LAN: encender otro equipo de casa con el "paquete magico".

Los equipos se configuran con WOL_DEVICES ("sobremesa=AA:BB:CC:DD:EE:FF;portatil=..."), asi el LLM
solo puede elegir un nombre de la lista, nunca una MAC cualquiera.
El paquete se manda desde el servidor y, si hay un PC con el HUD abierto, tambien desde el PC
(que esta en la misma red y no depende de la red interna de Docker).
"""

from __future__ import annotations

import re
import socket

from .registry import Tool, ToolContext, ToolError

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def parse_devices(raw: str) -> dict[str, str]:
    devices = {}
    for item in re.split(r"[;,]", raw or ""):
        if not item.strip():
            continue
        name, _, mac = item.partition("=")
        name, mac = name.strip().lower(), mac.strip()
        if not name or not MAC_RE.match(mac):
            raise ValueError(f"WOL_DEVICES: entrada no válida '{item.strip()}' (formato nombre=AA:BB:CC:DD:EE:FF)")
        devices[name] = mac.replace("-", ":").upper()
    return devices


def magic_packet(mac: str) -> bytes:
    if not MAC_RE.match(mac):
        raise ToolError(f"MAC no válida: {mac}")
    return b"\xff" * 6 + bytes.fromhex(re.sub(r"[:-]", "", mac)) * 16


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255") -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (9, 7):
            sock.sendto(packet, (broadcast, port))


def wol_tool(devices: dict[str, str], broadcast: str = "255.255.255.255", send=send_magic_packet) -> Tool:
    def run(ctx: ToolContext, device: str) -> str:
        mac = devices.get(device.lower())
        if mac is None:
            raise ToolError(f"no conozco el equipo '{device}'; equipos: {', '.join(devices)}")
        sent = []
        try:
            send(mac, broadcast)
            sent.append("el servidor")
        except OSError as exc:
            if ctx.pc_apps is None:
                raise ToolError(f"no se pudo enviar el paquete: {exc}") from exc
        if ctx.pc_apps is not None:
            ctx.pc_actions.append({"action": "wol", "mac": mac, "device": device.lower()})
            sent.append("el PC")
        return f"Paquete de encendido enviado a {device} desde {' y '.join(sent)}. Suele tardar un minuto en arrancar."

    return Tool(
        name="wake_on_lan",
        description="Enciende un equipo de casa por la red (Wake-on-LAN).",
        parameters={
            "type": "object",
            "properties": {"device": {"type": "string", "enum": sorted(devices)}},
            "required": ["device"],
        },
        fn=run,
    )
