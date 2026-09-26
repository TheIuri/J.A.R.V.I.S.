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
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON tal cual lo devuelve el modelo; lo valida el registro de tools


@dataclass(frozen=True)
class LLMMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class LLMReply:
    text: str
    provider: str
    tool_calls: tuple[ToolCall, ...] = ()


class OpenAICompatLLM:
    def __init__(self, cfg: LLMProviderConfig, timeout_s: int, max_tokens: int, client: httpx.Client | None = None):
        self.name = f"{cfg.name}:{cfg.model}"
        self.model = cfg.model
        self.max_tokens = max_tokens
        self.reasoning_effort = cfg.reasoning_effort
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self._client = client or httpx.Client(base_url=cfg.base_url, headers=headers, timeout=timeout_s)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMMessage:
        try:
            body = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
            if self.reasoning_effort:
                body["reasoning_effort"] = self.reasoning_effort
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            resp = self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            text = message.get("content")
            calls = tuple(
                ToolCall(c["id"], c["function"]["name"], c["function"].get("arguments") or "{}")
                for c in message.get("tool_calls") or []
            )
        except httpx.HTTPStatusError as exc:
            # El cuerpo explica el motivo (modelo inexistente, clave invalida, limite...).
            raise LLMError(f"{self.name}: HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"{self.name}: {exc}") from exc
        text = (text or "").strip()
        if not text and not calls:
            # Pasa con modelos que razonan si agotan max_tokens pensando.
            raise LLMError(f"{self.name}: respuesta vacia (sube LLM_MAX_TOKENS o baja el razonamiento)")
        return LLMMessage(text, calls)


class FallbackLLM:
    """Prueba los proveedores en orden. Util con capas gratuitas que tienen limites por minuto/dia."""

    def __init__(self, providers: list[OpenAICompatLLM]):
        if not providers:
            raise ValueError("Se necesita al menos un proveedor LLM")
        self.providers = providers

    @property
    def name(self) -> str:
        return " -> ".join(p.name for p in self.providers)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMReply:
        errors = []
        for provider in self.providers:
            try:
                msg = provider.chat(messages, tools)
                return LLMReply(msg.text, provider.name, msg.tool_calls)
            except LLMError as exc:
                log.warning("Fallo LLM, probando el siguiente: %s", exc)
                errors.append(str(exc))
        raise LLMError("Todos los proveedores LLM han fallado: " + " | ".join(errors))
