"""Cerebro. Todos los proveedores hablan la API de chat compatible con OpenAI
(Groq, Gemini, OpenRouter, Ollama), asi que cambiar de uno a otro es solo configuracion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import LLMProviderConfig

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class LLMReply:
    text: str
    provider: str


class OpenAICompatLLM:
    def __init__(self, cfg: LLMProviderConfig, timeout_s: int, max_tokens: int, client: httpx.Client | None = None):
        self.name = f"{cfg.name}:{cfg.model}"
        self.model = cfg.model
        self.max_tokens = max_tokens
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self._client = client or httpx.Client(base_url=cfg.base_url, headers=headers, timeout=timeout_s)

    def chat(self, messages: list[dict[str, str]]) -> str:
        try:
            resp = self._client.post(
                "/chat/completions",
                json={"model": self.model, "messages": messages, "max_tokens": self.max_tokens},
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            # El cuerpo explica el motivo (modelo inexistente, clave invalida, limite...).
            raise LLMError(f"{self.name}: HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"{self.name}: {exc}") from exc
        return (text or "").strip()


class FallbackLLM:
    """Prueba los proveedores en orden. Util con capas gratuitas que tienen limites por minuto/dia."""

    def __init__(self, providers: list[OpenAICompatLLM]):
        if not providers:
            raise ValueError("Se necesita al menos un proveedor LLM")
        self.providers = providers

    @property
    def name(self) -> str:
        return " -> ".join(p.name for p in self.providers)

    def chat(self, messages: list[dict[str, str]]) -> LLMReply:
        errors = []
        for provider in self.providers:
            try:
                return LLMReply(provider.chat(messages), provider.name)
            except LLMError as exc:
                log.warning("Fallo LLM, probando el siguiente: %s", exc)
                errors.append(str(exc))
        raise LLMError("Todos los proveedores LLM han fallado: " + " | ".join(errors))
