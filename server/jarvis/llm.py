"""Cerebro. Todos los proveedores hablan la API de chat compatible con OpenAI
(Groq, Gemini, Cerebras, Mistral, OpenRouter, Ollama), asi que cambiar de uno a otro es solo configuracion.

Las capas gratuitas tienen limites por minuto y por dia. Aqui se lleva la cuenta de cada modelo
(peticiones y tokens de hoy, lo que queda segun las cabeceras x-ratelimit-* del proveedor y el
ultimo 429) para el widget de cuota del HUD, y los agentes en segundo plano esperan y reintentan
cuando el proveedor dice "vuelve en X segundos".
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

from .config import LLMProviderConfig

log = logging.getLogger(__name__)

# Espera maxima que acepta un agente en segundo plano: los limites por minuto se liberan solos;
# los diarios piden horas y no merece la pena esperar.
MAX_PATIENCE_S = 45


class LLMError(RuntimeError):
    pass


class LLMRateLimit(LLMError):
    """429: limite por minuto o por dia. retry_after (segundos) si el proveedor lo dice."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


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


def _seconds(value: str) -> float | None:
    """"10.6s", "1m2.5s", "250ms", "42" -> segundos."""
    value = (value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"[\d.]+", value):
        return float(value)
    total, found = 0.0, False
    for number, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        found = True
        total += float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


def _retry_after(resp: httpx.Response) -> float | None:
    header = _seconds(resp.headers.get("retry-after", ""))
    if header is not None:
        return header
    # Groq: "Please try again in 10.6s" / "in 1m2.5s"; Gemini: "retryDelay": "31s"
    match = re.search(r'try again in ([\dhms.]+)|"retryDelay":\s*"([\d.]+s)"', resp.text)
    if match:
        return _seconds(match.group(1) or match.group(2))
    # Cerebras y otros: lo dicen en las cabeceras, p. ej. x-ratelimit-reset-requests-minute: 12.4
    resets = [
        wait for key, value in resp.headers.items()
        if key.lower().startswith("x-ratelimit-reset-") and "day" not in key.lower()
        and (wait := _seconds(value)) is not None and wait <= 120
    ]
    if resets:
        return max(resets)
    # Limite por minuto sin plazo: medio minuto suele bastar.
    return 30.0 if re.search(r"per minute|por minuto|\bRPM\b|\bTPM\b", resp.text, re.I) else None


# --- cuota ---------------------------------------------------------------------------------


def _window(provider: str, rest: str) -> str:
    """Periodo de un contador de x-ratelimit-*: "día", "min" o "" si no se sabe."""
    if "day" in rest:
        return "día"
    if "minute" in rest:
        return "min"
    if provider == "groq":  # Groq: peticiones por dia, tokens por minuto
        return "día" if rest.startswith("requests") else "min"
    return ""


@dataclass
class Usage:
    name: str
    provider: str
    day: date = field(default_factory=date.today)
    requests: int = 0
    tokens: int = 0
    errors: int = 0
    limited: int = 0  # 429 de hoy
    last_ok: float = 0.0
    last_error: str = ""
    last_error_at: float = 0.0
    blocked_until: float = 0.0  # epoch; > ahora = en su limite
    meters: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _roll(self) -> None:
        if self.day != date.today():
            self.day, self.requests, self.tokens, self.errors, self.limited = date.today(), 0, 0, 0, 0

    def headers(self, headers: httpx.Headers) -> None:
        now = time.time()
        for key, value in headers.items():
            match = re.fullmatch(r"x-ratelimit-(limit|remaining|reset)-(.+)", key.lower())
            if not match:
                continue
            kind, rest = match.groups()
            meter = self.meters.setdefault(rest, {"kind": "tokens" if "token" in rest else "requests",
                                                   "window": _window(self.provider, rest)})
            if kind == "reset":
                secs = _seconds(value)
                if secs is not None:
                    meter["reset_at"] = now + secs
            else:
                try:
                    meter[kind] = int(float(value))
                except ValueError:
                    pass

    def ok(self, tokens: int, headers: httpx.Headers) -> None:
        self._roll()
        self.requests += 1
        self.tokens += tokens
        self.last_ok = time.time()
        self.blocked_until = 0.0
        self.headers(headers)

    def fail(self, message: str, retry_after: float | None = None, headers: httpx.Headers | None = None) -> None:
        self._roll()
        self.errors += 1
        self.last_error = message[:200]
        self.last_error_at = time.time()
        if retry_after is not None or "429" in message:
            self.limited += 1
            # Sin plazo conocido (p. ej. cupo diario agotado) se aparta 5 minutos y se vuelve a probar.
            self.blocked_until = self.last_error_at + (retry_after if retry_after is not None else 300)
        if headers is not None:
            self.headers(headers)

    def can_take(self, need_tokens: int) -> bool:
        """False si esta en su limite o si, segun el proveedor, este minuto no le quedan tokens para la peticion."""
        now = time.time()
        if self.blocked_until > now:
            return False
        for meter in self.meters.values():
            if meter["kind"] == "tokens" and meter["window"] == "min" and meter.get("reset_at", 0) > now:
                remaining = meter.get("remaining")
                if remaining is not None and remaining < need_tokens:
                    return False
        return True

    def state(self) -> str:
        now = time.time()
        if self.blocked_until > now:
            return "limit"
        for meter in self.meters.values():
            limit, remaining = meter.get("limit"), meter.get("remaining")
            if limit and remaining is not None and meter.get("reset_at", now + 1) > now and remaining < limit * 0.2:
                return "low"
        if self.last_error_at > self.last_ok and now - self.last_error_at < 600:
            return "error"
        return "ok"

    def to_json(self) -> dict[str, Any]:
        self._roll()
        now = time.time()
        meters = []
        for rest, m in sorted(self.meters.items()):
            fresh = m.get("reset_at", now + 1) > now  # pasado el reset, el contador ya se ha renovado
            meters.append({
                "name": rest, "kind": m["kind"], "window": m["window"], "limit": m.get("limit"),
                "remaining": m.get("remaining") if fresh else m.get("limit"),
                "reset_in": max(0, round(m["reset_at"] - now)) if m.get("reset_at") and fresh else None,
            })
        return {
            "name": self.name, "provider": self.provider, "state": self.state(), "requests": self.requests,
            "tokens": self.tokens, "errors": self.errors, "limited": self.limited, "meters": meters,
            "blocked_for": max(0, round(self.blocked_until - now)) or None,
            "last_error": self.last_error, "last_ok": datetime.fromtimestamp(self.last_ok).isoformat(timespec="seconds")
            if self.last_ok else None,
        }


_USAGE: dict[str, Usage] = {}
_USAGE_LOCK = threading.Lock()


def usage_for(name: str, provider: str) -> Usage:
    with _USAGE_LOCK:
        return _USAGE.setdefault(name, Usage(name, provider))


def usage_report(names: list[str] | None = None) -> list[dict[str, Any]]:
    """Cuota de los modelos (los de names, en ese orden; o todos los que se han usado)."""
    with _USAGE_LOCK:
        items = [_USAGE[n] for n in names if n in _USAGE] if names else list(_USAGE.values())
    return [u.to_json() for u in items]


# --- proveedores -----------------------------------------------------------------------------


CATALOG_TTL_S = 600
MAX_CATALOG = 40
# Modelos que no son de conversacion (voz, embeddings, imagen, moderacion...).
_NOT_CHAT = re.compile(r"whisper|tts|embed|guard|moderation|image|dall-e|playai|orpheus|audio|rerank|transcri", re.I)


class OpenAICompatLLM:
    def __init__(self, cfg: LLMProviderConfig, timeout_s: int, max_tokens: int, client: httpx.Client | None = None):
        self.name = f"{cfg.name}:{cfg.model}"
        self.provider = cfg.name
        self.model = cfg.model
        self.max_tokens = max_tokens
        self.reasoning_effort = cfg.reasoning_effort
        self.usage = usage_for(self.name, self.provider)
        self.cfg = cfg
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        self._client = client or httpx.Client(base_url=cfg.base_url, headers=headers, timeout=timeout_s)
        self._catalog: tuple[float, list[str]] | None = None

    def with_model(self, model: str) -> OpenAICompatLLM:
        """El mismo proveedor (misma clave y conexion) con otro modelo de su catalogo."""
        effort = self.cfg.reasoning_effort if "gpt-oss" in model and "gpt-oss" in self.model else (
            "low" if "gpt-oss" in model and self.provider in ("groq", "cerebras") else "")
        cfg = LLMProviderConfig(self.cfg.name, self.cfg.base_url, self.cfg.api_key, model, effort)
        return OpenAICompatLLM(cfg, 0, self.max_tokens, client=self._client)

    def list_models(self) -> list[str]:
        """Modelos de chat que ofrece el proveedor (GET /models), cacheados 10 minutos. [] si no responde."""
        now = time.monotonic()
        if self._catalog and now - self._catalog[0] < CATALOG_TTL_S:
            return self._catalog[1]
        try:
            resp = self._client.get("/models", timeout=8)
            resp.raise_for_status()
            ids = [str(m.get("id") or "") for m in resp.json().get("data") or [] if isinstance(m, dict)]
        except (httpx.HTTPError, ValueError, AttributeError):
            ids = []
        ids = [i.removeprefix("models/") for i in ids if i and not _NOT_CHAT.search(i)]
        if self.provider == "openrouter":  # cientos de modelos: solo los gratis
            ids = [i for i in ids if i.endswith(":free")]
        ids = sorted(set(ids))[:MAX_CATALOG]
        self._catalog = (now, ids)
        return ids

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
            data = resp.json()
            message = data["choices"][0]["message"]
            text = message.get("content")
            calls = tuple(
                ToolCall(c["id"], c["function"]["name"], c["function"].get("arguments") or "{}")
                for c in message.get("tool_calls") or []
            )
        except httpx.HTTPStatusError as exc:
            detail = f"{self.name}: HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            if exc.response.status_code == 429:
                wait = _retry_after(exc.response)
                self.usage.fail(detail, wait, exc.response.headers)
                raise LLMRateLimit(detail, wait) from exc
            self.usage.fail(detail, headers=exc.response.headers)
            # El cuerpo explica el motivo (modelo inexistente, clave invalida, limite...).
            raise LLMError(detail) from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            self.usage.fail(f"{self.name}: {exc}")
            raise LLMError(f"{self.name}: {exc}") from exc
        tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        self.usage.ok(tokens, resp.headers)
        text = (text or "").strip()
        if not text and not calls:
            # Pasa con modelos que razonan si agotan max_tokens pensando.
            raise LLMError(f"{self.name}: respuesta vacia (sube LLM_MAX_TOKENS o baja el razonamiento)")
        return LLMMessage(text, calls)


class FallbackLLM:
    """Prueba los proveedores en orden. Util con capas gratuitas que tienen limites por minuto/dia."""

    def __init__(self, providers: list[OpenAICompatLLM], sleep=time.sleep):
        if not providers:
            raise ValueError("Se necesita al menos un proveedor LLM")
        self.providers = providers
        self._sleep = sleep
        self._extra: dict[str, OpenAICompatLLM] = {}  # modelos elegidos en el HUD fuera de la cadena

    @property
    def name(self) -> str:
        return " -> ".join(p.name for p in self.providers)

    def models(self, catalog: bool = False) -> list[dict[str, Any]]:
        """Para el selector del HUD: los modelos de la cadena y, con catalog, el resto de los que ofrece
        cada proveedor. id = "proveedor:modelo" (lo que se manda como prefer)."""
        out = [{"id": p.name, "provider": p.provider, "model": p.model, "chain": True} for p in self.providers]
        if catalog:
            seen = {p.name for p in self.providers}
            done: set[str] = set()
            for p in self.providers:
                if p.provider in done:
                    continue
                done.add(p.provider)
                for model in p.list_models():
                    name = f"{p.provider}:{model}"
                    if name not in seen:
                        seen.add(name)
                        out.append({"id": name, "provider": p.provider, "model": model, "chain": False})
        return out

    def _preferred(self, prefer: str | None) -> list[OpenAICompatLLM]:
        """El modelo elegido primero y el resto de la cadena de respaldo. prefer puede ser un proveedor
        ("groq", como antes) o un modelo ("opencode:big-pickle"), este de la cadena o de su catalogo."""
        if not prefer:
            return self.providers
        exact = [p for p in self.providers if p.name == prefer]
        if exact:
            return exact + [p for p in self.providers if p is not exact[0]]
        provider, _, model = prefer.partition(":")
        if model:
            base = next((p for p in self.providers if p.provider == provider), None)
            if base is not None and model in base.list_models():
                if prefer not in self._extra:
                    self._extra[prefer] = base.with_model(model)
                return [self._extra[prefer], *self.providers]
        return sorted(self.providers, key=lambda p: p.provider != provider)

    def chat(
        self, messages: list[dict], tools: list[dict] | None = None, prefer: str | None = None, patient: bool = False
    ) -> LLMReply:
        """prefer: proveedor elegido en el HUD; va primero y el resto quedan de respaldo.
        patient: si todos estan en su limite por minuto, espera lo que digan (hasta MAX_PATIENCE_S) y
        reintenta. Solo para trabajos en segundo plano (agentes); la conversacion no puede esperar."""
        preferred = self._preferred(prefer)
        # Tokens que pedira: ~4 caracteres por token de entrada, mas lo maximo que puede responder.
        prompt_tokens = sum(len(str(m.get("content") or "")) for m in messages) // 4 + len(str(tools or "")) // 4
        errors: list[str] = []
        for attempt in range(3 if patient else 1):
            # Rotacion: los que estan en su limite (o no les llega el cupo del minuto) pasan al final,
            # como ultimo recurso; asi no se gasta un intento en el que va a fallar.
            order = sorted(preferred, key=lambda p: not p.usage.can_take(prompt_tokens + p.max_tokens))
            errors, waits = [], []
            for provider in order:
                try:
                    msg = provider.chat(messages, tools)
                    return LLMReply(msg.text, provider.name, msg.tool_calls)
                except LLMError as exc:
                    log.warning("Fallo LLM, probando el siguiente: %s", exc)
                    errors.append(str(exc))
                    if isinstance(exc, LLMRateLimit) and exc.retry_after is not None:
                        waits.append(exc.retry_after)
            wait = min(waits) if waits else None
            if attempt == 2 or not patient or wait is None or wait > MAX_PATIENCE_S:
                break
            log.info("Todos los modelos en su limite por minuto: espero %.0f s y reintento", wait + 1)
            self._sleep(wait + 1)
        raise LLMError("Todos los proveedores LLM han fallado: " + " | ".join(errors))
