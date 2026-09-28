"""Descarga el binario oficial de Claude Code del registro de npm y comprueba su integridad (sha512).

Uso (en el Dockerfile): python install_claude.py <version|latest> <destino>
"""

import base64
import hashlib
import io
import json
import os
import platform
import sys
import tarfile
import urllib.request

REGISTRY = "https://registry.npmjs.org/@anthropic-ai/"
ARCH = {"x86_64": "linux-x64", "amd64": "linux-x64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as resp:
        return resp.read()


def main(version: str, dest: str) -> None:
    pkg = f"claude-code-{ARCH[platform.machine().lower()]}"
    meta = json.loads(fetch(f"{REGISTRY}{pkg}/{version}"))
    algo, _, expected = meta["dist"]["integrity"].partition("-")
    if algo != "sha512":
        raise SystemExit(f"integridad inesperada: {algo}")
    data = fetch(meta["dist"]["tarball"])
    if base64.b64encode(hashlib.sha512(data).digest()).decode() != expected:
        raise SystemExit("el paquete descargado no coincide con su sha512")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        binary = tar.extractfile("package/claude").read()
    with open(dest, "wb") as f:
        f.write(binary)
    os.chmod(dest, 0o755)
    print(f"Claude Code {meta['version']} ({pkg}) -> {dest}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
