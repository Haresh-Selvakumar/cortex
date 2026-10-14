"""Bounded provider adapters. No retries, credential logging or hidden fallback."""

from dataclasses import dataclass, asdict
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .policies import reserve_model_attempt, token_count

MAX_RESPONSE_BYTES = 256_000


class ProviderError(RuntimeError):
    def __init__(self, provider, category, status_code=None, retryable=False):
        # Never interpolate server bodies, credentials, URLs or exception text.
        super().__init__(f"{provider} provider: {category}")
        self.provider, self.category = provider, category
        self.status_code, self.retryable = status_code, retryable

    def to_dict(self):
        return {"provider": self.provider, "category": self.category,
                "status_code": self.status_code, "retryable": self.retryable,
                "automatic_retry": False}


@dataclass(frozen=True)
class ModelRequest:
    model: str
    system: str
    prompt: str
    schema: dict | None = None
    max_output_tokens: int = 1024
    prompt_version: str = "unspecified"


@dataclass(frozen=True)
class ModelResponse:
    provider: str
    model: str
    text: str
    usage: dict
    finish_reason: str | None
    response_id: str | None

    def to_dict(self):
        return asdict(self)


class Provider:
    """One generate() contract; concrete subclasses only map wire formats."""
    name = "abstract"
    default_url = ""
    credential_env = None

    def __init__(self, base_url=None, api_key_env=None, timeout=30, usage=None):
        self.base_url = (base_url or self.default_url).rstrip("/")
        parsed = urlsplit(self.base_url)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.hostname or parsed.scheme not in ("http", "https")
                or parsed.scheme == "http" and not local):
            raise ValueError("Provider endpoint must be HTTPS or local HTTP without credentials")
        # Cloud keys cannot be redirected to an arbitrary host by a config file.
        if self.name != "ollama" and self.base_url != self.default_url:
            raise ValueError("Cloud endpoint must use the official provider API")
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError("Provider timeout must be within 120 seconds")
        self.api_key_env = api_key_env or self.credential_env
        self.timeout = timeout
        self.usage = usage if usage is not None else []

    def configured(self):
        return self.api_key_env is None or bool(os.environ.get(self.api_key_env))

    def _redact(self, text):
        for name in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", self.api_key_env} - {None}:
            secret = os.environ.get(name)
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ProviderError(self.name, "credentials_missing")
            if self.name == "openai":
                headers["Authorization"] = "Bearer " + key
            else:
                headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        return headers

    def generate(self, request, budget, role="worker"):
        if (not isinstance(request, ModelRequest) or not isinstance(request.model, str)
                or not request.model.strip() or type(request.max_output_tokens) is not int
                or not 1 <= request.max_output_tokens <= 4096
                or type(request.system) is not str or type(request.prompt) is not str
                or len((request.system + request.prompt).encode()) > 64_000
                or request.schema is not None and type(request.schema) is not dict):
            raise ProviderError(self.name, "invalid_request")
        if budget is not None and hasattr(budget, "replay_provider"):
            cached = budget.replay_provider(self.name, request, role)
            if cached is not None:
                return cached
        body, endpoint = self._body(request)
        wire = urllib.request.Request(self.base_url + endpoint, data=json.dumps(body).encode(),
                                      headers=self._headers())
        record = {"provider": self.name, "model": self._redact(request.model), "role": role,
                  "prompt_version": request.prompt_version,
                  "prompt_sha256": hashlib.sha256((request.system + "\n" + request.prompt).encode()).hexdigest(),
                  "prompt_tokens": None, "output_tokens": None, "status": "attempted"}
        reserve_model_attempt(budget)
        self.usage.append(record)
        if budget is not None and hasattr(budget, "before_provider_call"):
            budget.before_provider_call(record, self.usage)
        start = time.perf_counter()
        normalized_response = None
        try:
            try:
                with urllib.request.urlopen(wire, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ProviderError(self.name, "response_too_large")
                payload = json.loads(raw)
                if type(payload) is not dict:
                    raise ProviderError(self.name, "invalid_response")
            except urllib.error.HTTPError as exc:
                code = exc.code
                category = ("authentication" if code in (401, 403) else
                            "rate_limit" if code == 429 else "model_unavailable" if code == 404 else
                            "server_error" if code >= 500 else "invalid_request")
                raise ProviderError(self.name, category, code, code == 429 or code >= 500) from None
            except TimeoutError:
                raise ProviderError(self.name, "timeout", retryable=True) from None
            except urllib.error.URLError as exc:
                category = "timeout" if isinstance(exc.reason, TimeoutError) else "connection"
                raise ProviderError(self.name, category, retryable=True) from None
            except (ValueError, UnicodeError):
                raise ProviderError(self.name, "invalid_response") from None
            except OSError:
                raise ProviderError(self.name, "connection", retryable=True) from None

            usage = self._usage(payload)
            record.update(prompt_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                          usage=usage, status="response_received")
            try:
                text, finish = self._text(payload)
                if type(text) is not str or not text.strip():
                    raise ProviderError(self.name, "invalid_response")
                text = self._redact(text)
                actual_model = payload.get("model")
                actual_model = actual_model if isinstance(actual_model, str) and actual_model else request.model
                actual_model = self._redact(actual_model)
                identifier = payload.get("id")
                identifier = identifier if isinstance(identifier, str) else None
                identifier = self._redact(identifier) if identifier else None
            except (KeyError, TypeError, AttributeError):
                raise ProviderError(self.name, "invalid_response") from None
            record.update(status="ok", provider_model=actual_model, response_id=identifier,
                          model_output_sha256=hashlib.sha256(text.encode()).hexdigest())
            normalized_response = ModelResponse(self.name, actual_model, text, usage, finish, identifier)
            return normalized_response
        except ProviderError as exc:
            record.update(status="failed", error=exc.to_dict())
            raise
        finally:
            record["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
            if budget is not None and hasattr(budget, "after_provider_call"):
                budget.after_provider_call(record, normalized_response)

    def _usage(self, payload):
        raw = payload.get("usage")
        raw = raw if type(raw) is dict else {}
        return {"input_tokens": token_count(raw.get("input_tokens")),
                "provider_reported_input_tokens": token_count(raw.get("input_tokens")),
                "output_tokens": token_count(raw.get("output_tokens")),
                "cached_input_tokens": None, "cache_creation_input_tokens": None}


class OllamaProvider(Provider):
    name, default_url = "ollama", "http://localhost:11434"

    def _body(self, request):
        body = {"model": request.model, "stream": False, "think": False,
                "messages": [{"role": "system", "content": request.system},
                             {"role": "user", "content": request.prompt}],
                "options": {"temperature": 0, "num_predict": request.max_output_tokens}}
        if request.schema is not None:
            body["format"] = request.schema
        return body, "/api/chat"

    def _usage(self, payload):
        return {"input_tokens": token_count(payload.get("prompt_eval_count")),
                "provider_reported_input_tokens": token_count(payload.get("prompt_eval_count")),
                "output_tokens": token_count(payload.get("eval_count")),
                "cached_input_tokens": None, "cache_creation_input_tokens": None}

    def _text(self, payload):
        if payload.get("done") is not True:
            raise ProviderError(self.name, "incomplete")
        reason = payload.get("done_reason")
        if reason == "length":
            raise ProviderError(self.name, "incomplete")
        message = payload["message"]
        if message.get("tool_calls"):
            raise ProviderError(self.name, "tool_execution_not_supported")
        return message["content"], reason


class OpenAIProvider(Provider):
    name, default_url, credential_env = "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"

    def _body(self, request):
        body = {"model": request.model, "instructions": request.system, "input": request.prompt,
                "max_output_tokens": request.max_output_tokens, "store": False}
        if request.schema is not None:
            body["text"] = {"format": {"type": "json_schema", "name": "cortex_response",
                                        "schema": request.schema, "strict": True}}
        return body, "/responses"

    def _usage(self, payload):
        usage = super()._usage(payload)
        raw = payload.get("usage")
        raw = raw if type(raw) is dict else {}
        details = raw.get("input_tokens_details")
        details = details if type(details) is dict else {}
        usage["cached_input_tokens"] = token_count(details.get("cached_tokens"))
        return usage

    def _text(self, payload):
        if payload.get("status") != "completed":
            raise ProviderError(self.name, "incomplete" if payload.get("status") == "incomplete" else "response_failed")
        parts = []
        for item in payload["output"]:
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message":
                raise ProviderError(self.name, "tool_execution_not_supported")
            for content in item["content"]:
                if content.get("type") == "refusal":
                    raise ProviderError(self.name, "refused")
                if content.get("type") != "output_text":
                    raise ProviderError(self.name, "invalid_response")
                parts.append(content["text"])
        return "".join(parts), payload["status"]


class AnthropicProvider(Provider):
    name, default_url, credential_env = "anthropic", "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"

    def _body(self, request):
        body = {"model": request.model, "system": request.system, "max_tokens": request.max_output_tokens,
                "messages": [{"role": "user", "content": request.prompt}], "stream": False}
        if request.schema is not None:
            body["output_config"] = {"format": {"type": "json_schema", "schema": request.schema}}
        return body, "/messages"

    def _usage(self, payload):
        usage = super()._usage(payload)
        raw = payload.get("usage")
        raw = raw if type(raw) is dict else {}
        usage.update(cached_input_tokens=token_count(raw.get("cache_read_input_tokens")),
                     cache_creation_input_tokens=token_count(raw.get("cache_creation_input_tokens")))
        # Anthropic's ordinary input count excludes cache reads/creation.
        parts = (usage["provider_reported_input_tokens"], usage["cached_input_tokens"], usage["cache_creation_input_tokens"])
        usage["input_tokens"] = sum(parts) if all(value is not None for value in parts) else None
        return usage

    def _text(self, payload):
        reason = payload.get("stop_reason")
        if reason == "refusal":
            raise ProviderError(self.name, "refused")
        if reason not in ("end_turn", "stop_sequence"):
            raise ProviderError(self.name, "tool_execution_not_supported" if reason == "tool_use" else "incomplete")
        parts = []
        for item in payload["content"]:
            if item.get("type") in ("thinking", "redacted_thinking"):
                continue
            if item.get("type") != "text":
                raise ProviderError(self.name, "tool_execution_not_supported")
            parts.append(item["text"])
        return "".join(parts), reason


PROVIDERS = {provider.name: provider for provider in (OllamaProvider, OpenAIProvider, AnthropicProvider)}
