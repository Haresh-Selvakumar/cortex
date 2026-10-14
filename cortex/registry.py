"""Configured model capabilities, not inferred vendor-wide competence."""

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re

from .providers import PROVIDERS

TASK_TYPES = ("analysis", "coding", "documents", "scheduling", "mixed")
CAPABILITIES = ("analysis", "supplier_analysis", "quote_extraction", "classification",
                "text_generation", "structured_output", "code_generation", "code_execution",
                "document_draft", "file_write", "scheduling", "reminders", "github", "web_access")
ROLES = ("classifier", "planner", "worker")
PRICE_KEYS = ("input_usd_per_million", "output_usd_per_million",
              "cached_input_usd_per_million", "cache_creation_usd_per_million")


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    model: str | None
    task_types: tuple
    capabilities: tuple
    structured_output: bool
    tools: bool
    enabled: bool
    priority: int
    pricing: dict

    def to_dict(self):
        return asdict(self)


class Registry:
    def __init__(self, payload):
        if (type(payload) is not dict or type(payload.get("version")) is not int or payload.get("version") != 1
                or set(payload) - {"version", "providers", "models", "roles"}):
            raise ValueError("Invalid registry configuration")
        providers = payload.get("providers", {})
        if type(providers) is not dict or set(providers) - set(PROVIDERS):
            raise ValueError("Unknown configured provider")
        self.providers = {}
        for name, settings in providers.items():
            if type(settings) is not dict or set(settings) - {"base_url", "timeout", "api_key_env"}:
                raise ValueError("Provider configuration accepts endpoint, timeout and credential environment name only")
            if "api_key_env" in settings and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", settings["api_key_env"]):
                raise ValueError("Invalid credential environment name")
            PROVIDERS[name](**settings)  # Validate endpoint without reading/logging key values.
            self.providers[name] = dict(settings)
        self.models = {}
        rows = payload.get("models")
        if type(rows) is not list or not 1 <= len(rows) <= 64:
            raise ValueError("Configure one to 64 models")
        for row in rows:
            allowed = {"id", "provider", "model", "model_env", "task_types", "capabilities",
                       "structured_output", "tools", "enabled", "priority", "pricing"}
            if type(row) is not dict or set(row) - allowed:
                raise ValueError("Invalid model fields")
            identifier = row.get("id")
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier) or identifier in self.models:
                raise ValueError("Model identifiers must be unique bounded labels")
            provider = row.get("provider")
            if type(provider) is not str or provider not in self.providers:
                raise ValueError("Model provider must be configured")
            if ("model" in row) == ("model_env" in row):
                raise ValueError("Specify model or model_env, separately from credentials")
            if "model_env" in row:
                variable = row["model_env"]
                if not isinstance(variable, str) or not re.fullmatch(r"CORTEX_[A-Z0-9_]+_MODEL", variable):
                    raise ValueError("Model environment names must follow CORTEX_*_MODEL")
                model = os.environ.get(variable) or None
            else:
                model = row["model"]
            if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200):
                raise ValueError("Invalid provider model identifier")
            secret_names = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"} | {p.get("api_key_env") for p in self.providers.values()}
            if model and any(os.environ.get(name) == model for name in secret_names if name):
                raise ValueError("A credential cannot be used as a model identifier")
            task_types, capabilities = row.get("task_types"), row.get("capabilities")
            if (type(task_types) is not list or not task_types or any(type(x) is not str or x not in TASK_TYPES for x in task_types)
                    or len(set(task_types)) != len(task_types)
                    or type(capabilities) is not list or not capabilities
                    or any(type(x) is not str or x not in CAPABILITIES for x in capabilities)
                    or len(set(capabilities)) != len(capabilities)):
                raise ValueError("Invalid task types or capabilities")
            structured, tools, enabled = row.get("structured_output"), row.get("tools"), row.get("enabled", True)
            priority = row.get("priority", 100)
            if (any(type(value) is not bool for value in (structured, tools, enabled))
                    or type(priority) is not int or not 0 <= priority <= 1000
                    or "structured_output" in capabilities and not structured):
                raise ValueError("Invalid capability flags or priority")
            pricing = row.get("pricing", {})
            if type(pricing) is not dict or set(pricing) - set(PRICE_KEYS):
                raise ValueError("Pricing uses configured USD rates per million tokens")
            prices = {}
            for key in PRICE_KEYS:
                value = pricing.get(key)
                if value is None:
                    prices[key] = None
                    continue
                try:
                    number = Decimal(str(value))
                except InvalidOperation:
                    raise ValueError("Invalid configured price") from None
                if type(value) is bool or not number.is_finite() or number < 0:
                    raise ValueError("Configured prices must be finite and nonnegative")
                prices[key] = str(number)
            self.models[identifier] = ModelSpec(identifier, provider, model, tuple(task_types), tuple(capabilities),
                                                 structured, tools, enabled, priority, prices)
        self.roles = payload.get("roles", {})
        if type(self.roles) is not dict or set(self.roles) - set(ROLES):
            raise ValueError("Unknown configured role")
        if any(value is not None and (type(value) is not str or value not in self.models) for value in self.roles.values()):
            raise ValueError("Role references an unregistered model")

    @classmethod
    def load(cls, path):
        path = Path(path)
        if path.stat().st_size > 64_000:
            raise ValueError("Registry exceeds supported configuration size")
        return cls(json.loads(path.read_text(encoding="utf-8")))


class Runtime:
    """One usage ledger and shared Budget; availability evidence is explicit."""
    def __init__(self, registry, offline=False):
        self.registry, self.offline = registry, offline
        self.usage = []
        self.availability = {}

    def provider(self, spec):
        return PROVIDERS[spec.provider](**self.registry.providers[spec.provider], usage=self.usage)

    def available(self, spec):
        if spec.id in self.availability:
            return self.availability[spec.id]
        reason = ("disabled" if not spec.enabled else "model_not_configured" if not spec.model else
                  "offline_mode" if self.offline else
                  "credentials_missing" if not self.provider(spec).configured() else "configured_not_probed")
        return {"available": reason == "configured_not_probed", "reason": reason,
                "connectivity_verified": False}

    def probe(self):
        # Metadata requests are not model invocations and do not consume call caps.
        import urllib.request
        from .providers import ProviderError, MAX_RESPONSE_BYTES
        inventories = {}
        for spec in self.registry.models.values():
            configured = self.available(spec)
            if not configured["available"]:
                self.availability[spec.id] = configured
                continue
            provider = self.provider(spec)
            if spec.provider not in inventories:
                try:
                    endpoint = "/api/tags" if spec.provider == "ollama" else "/models"
                    wire = urllib.request.Request(provider.base_url + endpoint, headers=provider._headers())
                    with urllib.request.urlopen(wire, timeout=min(provider.timeout, 3)) as response:
                        raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError("Inventory bound")
                    payload = json.loads(raw)
                    rows = payload["models" if spec.provider == "ollama" else "data"]
                    names = {row["name" if spec.provider == "ollama" else "id"] for row in rows}
                    inventories[spec.provider] = names
                except Exception as exc:
                    # Diagnosis uses types/status, never exception text or body.
                    import urllib.error
                    category = ("http_error" if isinstance(exc, urllib.error.HTTPError) else
                                "timeout" if isinstance(exc, TimeoutError) else "inventory_unavailable")
                    inventories[spec.provider] = {"error": category}
            inventory = inventories[spec.provider]
            failed = isinstance(inventory, dict)
            present = not failed and (spec.model in inventory or spec.provider == "ollama" and spec.model + ":latest" in inventory)
            self.availability[spec.id] = {"available": present,
                "reason": inventory["error"] if failed else "model_listed" if present else "model_not_listed",
                "connectivity_verified": not failed}
