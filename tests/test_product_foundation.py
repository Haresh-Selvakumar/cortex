"""Mocked provider contracts and real deterministic supplier integration."""

from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from cortex.classifier import Classification, classify, supplier_intent
from cortex.domain import Task
from cortex.policies import Budget, ModelCallLimit
from cortex.product import execute_request
from cortex.product_cli import main
from cortex.providers import (AnthropicProvider, ModelRequest, OllamaProvider,
                              OpenAIProvider, ProviderError)
from cortex.registry import Registry, Runtime
from cortex.routing import route

ROOT = Path(__file__).resolve().parents[1]
HTTP = "urllib.request.urlopen"


def registry_payload():
    return json.loads((ROOT / "config/cortex.example.json").read_text())


def registry():
    return Registry(registry_payload())


def supplier_task(name="supplier_task.json"):
    return Task.from_dict(json.loads((ROOT / "examples" / name).read_text()))


def classification(kind="analysis", caps=None, uncertain=False, missing=None, decomposition=False):
    return {"task_type": kind, "required_capabilities": caps or ["analysis", "supplier_analysis"],
            "missing_essential_information": missing or [], "needs_decomposition": decomposition, "uncertain": uncertain}


def reply(provider="ollama", content="hello", usage=True, **extra):
    text = json.dumps(content) if not isinstance(content, str) else content
    if provider == "ollama":
        payload = {"model": "reported-model", "message": {"content": text}, "done": True, "done_reason": "stop"}
        if usage:
            payload.update(prompt_eval_count=12, eval_count=4)
    elif provider == "openai":
        payload = {"id": "resp_mock", "model": "reported-model", "status": "completed",
                   "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        if usage:
            payload["usage"] = {"input_tokens": 12, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 0}}
    else:
        payload = {"id": "msg_mock", "model": "reported-model", "stop_reason": "end_turn",
                   "content": [{"type": "text", "text": text}]}
        if usage:
            payload["usage"] = {"input_tokens": 12, "output_tokens": 4,
                                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    if type(usage) is dict:
        payload["usage"] = usage
    payload.update(extra)
    return io.BytesIO(json.dumps(payload).encode())


def quote_response(task):
    # Independent fixture from raw structured source, not extracted by the model.
    rows = json.loads(task.source)
    for row in rows:
        row["evidence"] = {field: [task.source] for field in (
            "supplier", "unit_price", "shipping", "quantity_available", "delivery_days")}
        row["issues"] = []
    return {"quotes": rows}


class ProviderContractTests(unittest.TestCase):
    def test_interchangeable_response_usage_and_request_wire_contracts(self):
        schema = {"type": "object", "properties": {"answer": {"type": "string"}},
                  "required": ["answer"], "additionalProperties": False}
        for cls in (OllamaProvider, OpenAIProvider, AnthropicProvider):
            with self.subTest(provider=cls.name), patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key", "ANTHROPIC_API_KEY": "fake-key"}), \
                    patch(HTTP, return_value=reply(cls.name, {"answer": "ok"})) as send:
                provider, budget = cls(), Budget(max_model_calls=1)
                response = provider.generate(ModelRequest("explicit-model", "system", "input", schema), budget, "classifier")
            self.assertEqual(response.provider, cls.name)
            self.assertEqual(response.model, "reported-model")
            self.assertEqual(json.loads(response.text), {"answer": "ok"})
            self.assertEqual(response.usage["input_tokens"], 12)
            self.assertEqual(response.usage["output_tokens"], 4)
            self.assertEqual(budget.model_calls, 1)
            self.assertEqual(provider.usage[0]["role"], "classifier")
            wire = send.call_args.args[0]
            body = json.loads(wire.data)
            if cls.name == "ollama":
                self.assertEqual(body["format"], schema)
                self.assertTrue(wire.full_url.endswith("/api/chat"))
            elif cls.name == "openai":
                self.assertEqual(body["text"]["format"]["schema"], schema)
                self.assertFalse(body["store"])
                self.assertTrue(wire.full_url.endswith("/v1/responses"))
            else:
                self.assertEqual(body["output_config"]["format"]["schema"], schema)
                self.assertEqual(wire.get_header("Anthropic-version"), "2023-06-01")

    def test_missing_credentials_prevents_attempt_and_key_never_enters_usage(self):
        with patch.dict(os.environ, {}, clear=True), patch(HTTP) as send:
            provider, budget = OpenAIProvider(), Budget()
            with self.assertRaises(ProviderError) as raised:
                provider.generate(ModelRequest("model", "s", "p"), budget)
        self.assertEqual(raised.exception.category, "credentials_missing")
        self.assertEqual(budget.model_calls, 0)
        self.assertEqual(provider.usage, [])
        send.assert_not_called()

    def test_structural_http_categories_are_counted_without_retry_or_body_leak(self):
        for status, category in ((400, "invalid_request"), (401, "authentication"),
                                 (403, "authentication"), (404, "model_unavailable"),
                                 (429, "rate_limit"), (503, "server_error")):
            failure = urllib.error.HTTPError("http://localhost:11434/api/chat", status,
                                            "secret text timeout 401", {}, io.BytesIO(b"SECRET-KEY"))
            provider, budget = OllamaProvider(), Budget(max_model_calls=4)
            with self.subTest(status=status), patch(HTTP, side_effect=failure) as send, self.assertRaises(ProviderError) as raised:
                provider.generate(ModelRequest("model", "s", "p"), budget)
            self.assertEqual(raised.exception.category, category)
            self.assertEqual(raised.exception.status_code, status)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(budget.model_calls, 1)
            self.assertIsNone(provider.usage[0]["prompt_tokens"])
            self.assertNotIn("SECRET", json.dumps(provider.usage))

    def test_timeout_connection_and_malformed_response_normalise(self):
        for failure, category in ((TimeoutError("auth"), "timeout"),
                                  (urllib.error.URLError(TimeoutError("auth")), "timeout"),
                                  (urllib.error.URLError("timeout"), "connection")):
            with self.subTest(category=category), patch(HTTP, side_effect=failure), self.assertRaises(ProviderError) as raised:
                OllamaProvider().generate(ModelRequest("model", "s", "p"), Budget())
            self.assertEqual(raised.exception.category, category)
        for raw in (b"bad JSON", b"[]", b"x" * 256001):
            with self.subTest(raw_size=len(raw)), patch(HTTP, return_value=io.BytesIO(raw)), self.assertRaises(ProviderError):
                OllamaProvider().generate(ModelRequest("model", "s", "p"), Budget())

    def test_unknown_and_invalid_token_counts_stay_unknown(self):
        with patch(HTTP, return_value=reply(usage=False)):
            response = OllamaProvider().generate(ModelRequest("model", "s", "p"), Budget())
        self.assertIsNone(response.usage["input_tokens"])
        with patch(HTTP, return_value=reply(prompt_eval_count=True, eval_count=-1)):
            response = OllamaProvider().generate(ModelRequest("model", "s", "p"), Budget())
        self.assertIsNone(response.usage["input_tokens"])
        self.assertIsNone(response.usage["output_tokens"])

    def test_anthropic_cache_usage_is_included_without_assuming_unknown_cache_zero(self):
        for cache, expected in (({"cache_read_input_tokens": 5, "cache_creation_input_tokens": 3}, 20),
                                ({}, None)):
            with self.subTest(cache=cache), patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake-key"}), \
                    patch(HTTP, return_value=reply("anthropic", usage={"input_tokens": 12, "output_tokens": 4, **cache})):
                response = AnthropicProvider().generate(ModelRequest("model", "s", "p"), Budget())
            self.assertEqual(response.usage["input_tokens"], expected)
            self.assertEqual(response.usage["provider_reported_input_tokens"], 12)

    def test_successful_response_cannot_echo_environment_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SENSITIVE-TEST-KEY"}), \
                patch(HTTP, return_value=reply(content="Echo SENSITIVE-TEST-KEY", model="SENSITIVE-TEST-KEY")):
            provider = OllamaProvider()
            response = provider.generate(ModelRequest("configured-model", "s", "p"), Budget())
        self.assertNotIn("SENSITIVE-TEST-KEY", json.dumps(response.to_dict()))
        self.assertNotIn("SENSITIVE-TEST-KEY", json.dumps(provider.usage))

    def test_shared_cap_and_invalid_request_prevent_http(self):
        provider = OllamaProvider()
        for budget, request, error in ((Budget(max_model_calls=0), ModelRequest("model", "s", "p"), ModelCallLimit),
                                       (Budget(), ModelRequest("", "s", "p"), ProviderError)):
            with patch(HTTP) as send, self.assertRaises(error):
                provider.generate(request, budget)
            send.assert_not_called()
        self.assertEqual(provider.usage, [])

    def test_incomplete_refusal_and_tool_calls_are_not_completed_text(self):
        cases = [(OllamaProvider, reply(done_reason="length"), "incomplete"),
                 (OpenAIProvider, reply("openai", status="incomplete"), "incomplete"),
                 (OpenAIProvider, reply("openai", output=[{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]), "refused"),
                 (AnthropicProvider, reply("anthropic", stop_reason="max_tokens"), "incomplete"),
                 (AnthropicProvider, reply("anthropic", stop_reason="tool_use"), "tool_execution_not_supported")]
        for cls, body, category in cases:
            with self.subTest(provider=cls.name), patch.dict(os.environ, {"OPENAI_API_KEY": "fake-key", "ANTHROPIC_API_KEY": "fake-key"}), \
                    patch(HTTP, return_value=body), self.assertRaises(ProviderError) as raised:
                cls().generate(ModelRequest("model", "s", "p"), Budget())
            self.assertEqual(raised.exception.category, category)

    def test_cloud_credentials_cannot_be_redirected_to_custom_host(self):
        for cls in (OpenAIProvider, AnthropicProvider):
            with self.assertRaises(ValueError):
                cls(base_url="https://example.com/v1")
        for url in ("http://example.com", "https://key@example.com", "http://localhost:11434?key=secret"):
            with self.assertRaises(ValueError):
                OllamaProvider(base_url=url)


class ClassificationAndRoutingTests(unittest.TestCase):
    def test_clear_supported_supplier_uses_rule_and_zero_calls(self):
        runtime, budget, trace = Runtime(registry(), offline=True), Budget(max_model_calls=0), []
        with patch(HTTP) as send:
            result = classify("Compare suppliers", supplier_task(), runtime, budget, trace)
        send.assert_not_called()
        self.assertFalse(result.uncertain)
        self.assertIn("supplier_analysis", result.required_capabilities)
        self.assertEqual(trace[0]["decision_source"], "rule")
        self.assertFalse(supplier_intent("Compare suppliers and delete my repository"))

    def test_ai_structured_classification_with_separate_role_config(self):
        runtime, trace, budget = Runtime(registry()), [], Budget(max_model_calls=1)
        with patch(HTTP, return_value=reply(content=classification("coding", ["code_generation"]))) as send:
            result = classify("Write a Python function", None, runtime, budget, trace)
        self.assertEqual(result.task_type, "coding")
        self.assertEqual(json.loads(send.call_args.args[0].data)["model"], "qwen2.5:3b-instruct")
        self.assertEqual(runtime.usage[0]["role"], "classifier")
        self.assertEqual(budget.model_calls, 1)

    def test_classifier_rejects_confidence_wrong_types_extra_or_missing_fields(self):
        invalid = [classification(uncertain=0), classification(caps=["teleport"]),
                   classification("coding", ["supplier_analysis"]), classification(missing=[" "]),
                   classification("mixed", ["text_generation"], decomposition=False)]
        invalid += [{**classification(), "confidence": .99}, {"task_type": "analysis"},
                    classification(caps=["supplier_analysis", "classification"])]
        for proposal in invalid:
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                Classification.validate(proposal)

    def test_invalid_or_unavailable_classifier_explicitly_clarifies(self):
        for body in (classification(uncertain=True), {"confidence": 1}, "not JSON"):
            with self.subTest(body=body), patch(HTTP, return_value=reply(content=body)) as send:
                result = execute_request("Do something", registry(), classifier_mode="ai")
            self.assertEqual(result["status"], "needs_clarification")
            self.assertEqual(send.call_count, 1)
            self.assertFalse(any(event["stage"] == "routing" for event in result["trace"]))
        with patch(HTTP) as send:
            result = execute_request("Write a Python function", registry(), offline=True)
        send.assert_not_called()
        self.assertEqual(result["trace"][0]["fallback"], "request_clarification")

    def test_missing_essential_info_and_decomposition_stop_before_worker(self):
        with patch(HTTP) as send:
            result = execute_request("Compare suppliers", registry(), offline=True)
        send.assert_not_called()
        self.assertEqual(result["status"], "needs_clarification")
        self.assertTrue(result["classification"]["missing_essential_information"])
        proposal = classification("mixed", ["code_generation", "document_draft"], decomposition=True)
        with patch(HTTP, return_value=reply(content=proposal)) as send:
            result = execute_request("Write code and a report", registry(), classifier_mode="ai")
        self.assertEqual(result["status"], "needs_planning")
        self.assertEqual(send.call_count, 1)
        self.assertFalse(result["roles"]["planner_invoked"])

    def test_no_missing_sentinel_normalises_but_mixed_missing_claim_is_rejected(self):
        with patch(HTTP, return_value=reply(content=classification(missing=["none"]))):
            result = execute_request("Compare suppliers", registry(), supplier_task(),
                                     Budget(max_model_calls=1), classifier_mode="ai")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["trace"][0]["normalization_applied"])
        self.assertEqual(result["trace"][0]["proposed_classification"]["missing_essential_information"], ["none"])
        with self.assertRaises(ValueError):
            Classification.validate(classification(missing=["none", "source"]))

    def test_execution_capabilities_are_not_inferred_from_model_tool_support(self):
        payload = registry_payload()
        payload["models"][1]["tools"] = True
        payload["models"][1]["task_types"].append("scheduling")
        payload["models"][1]["capabilities"].append("reminders")
        runtime, trace = Runtime(Registry(payload)), []
        requirement = Classification.validate(classification("scheduling", ["reminders"]))
        worker = route(requirement, None, runtime, Budget(), trace)
        self.assertIsNone(worker)
        self.assertTrue(any("missing_capabilities:reminders" in row["rejections"] for row in trace[0]["eligibility"]))

    def test_model_eligibility_availability_priority_and_override(self):
        runtime, trace = Runtime(registry()), []
        requirement = Classification.validate(classification("coding", ["code_generation"]))
        worker = route(requirement, None, runtime, Budget(), trace)
        self.assertEqual(worker.model_id, "local-worker")
        self.assertFalse(next(row for row in trace[0]["eligibility"] if row["model_id"] == "local-classifier")["eligible"])
        trace = []
        self.assertIsNone(route(requirement, None, runtime, Budget(), trace, override="local-classifier"))
        self.assertIsNone(route(requirement, None, Runtime(registry(), offline=True), Budget(), []))

    def test_budget_filter_blocks_models_but_permits_deterministic_worker(self):
        requirement = Classification.validate(classification())
        worker = route(requirement, supplier_task(), Runtime(registry()), Budget(max_model_calls=0), [])
        self.assertEqual(worker.kind, "supplier_parser")
        self.assertIsNone(route(requirement, supplier_task(), Runtime(registry()), Budget(max_units=0), []))
        self.assertIsNone(route(requirement, supplier_task(), Runtime(registry()), Budget(max_model_calls=0), [], override="local-worker"))

    def test_capability_registry_retains_unknown_and_configured_prices(self):
        payload = registry_payload()
        payload["models"][1]["pricing"] = {"input_usd_per_million": "0.75", "output_usd_per_million": None}
        spec = Registry(payload).models["local-worker"]
        self.assertEqual(spec.pricing["input_usd_per_million"], "0.75")
        self.assertIsNone(spec.pricing["output_usd_per_million"])
        for invalid in (True, -1, "NaN", "Infinity"):
            payload["models"][1]["pricing"]["input_usd_per_million"] = invalid
            with self.assertRaises(ValueError):
                Registry(payload)

    def test_registry_rejects_duplicate_models_credentials_and_bad_roles(self):
        for mutation in (lambda p: p["models"].append(deepcopy(p["models"][0])),
                         lambda p: p["providers"]["openai"].update(api_key="do-not-store"),
                         lambda p: p["roles"].update(classifier="nonexistent"),
                         lambda p: p["models"][0].update(structured_output=False)):
            payload = registry_payload()
            mutation(payload)
            with self.assertRaises(ValueError):
                Registry(payload)

    def test_provider_inventory_probe_and_offline_skips_metadata(self):
        runtime = Runtime(registry())
        body = io.BytesIO(json.dumps({"models": [{"name": "qwen2.5:3b-instruct"}]}).encode())
        with patch(HTTP, return_value=body) as send:
            runtime.probe()
        self.assertEqual(send.call_count, 1)
        self.assertTrue(runtime.available(runtime.registry.models["local-classifier"])["connectivity_verified"])
        self.assertFalse(runtime.available(runtime.registry.models["local-worker"])["available"])
        with patch(HTTP) as send:
            Runtime(registry(), offline=True).probe()
        send.assert_not_called()


class ProductExecutionTests(unittest.TestCase):
    def test_request_help_works_with_windows_console_encoding(self):
        raw = io.BytesIO()
        console = io.TextIOWrapper(raw, encoding="cp1252", write_through=True)
        with contextlib.redirect_stdout(console), self.assertRaises(SystemExit) as exit:
            main(["--help"])
        self.assertEqual(exit.exception.code, 0)
        self.assertIn(b"--classifier-model", raw.getvalue())

    def test_cloud_classifier_roles_are_interchangeable_with_mocked_http(self):
        for provider, variable, identifier in (("openai", "CORTEX_OPENAI_MODEL", "openai-configured"),
                                              ("anthropic", "CORTEX_ANTHROPIC_MODEL", "anthropic-configured")):
            env = {variable: "configured-test-model", "OPENAI_API_KEY": "fake-key", "ANTHROPIC_API_KEY": "fake-key"}
            with self.subTest(provider=provider), patch.dict(os.environ, env), \
                    patch(HTTP, return_value=reply(provider, classification())) as send:
                result = execute_request("Compare suppliers", registry(), supplier_task(), Budget(max_model_calls=1),
                                         classifier_mode="ai", classifier_model=identifier)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(result["model_usage"][0]["provider"], provider)
            self.assertEqual(result["model_usage"][0]["role"], "classifier")

    def test_classifier_router_supplier_evidence_preserves_core_result(self):
        with patch(HTTP) as send:
            result = execute_request("Compare suppliers", registry(), supplier_task(), Budget(max_model_calls=0), offline=True)
        send.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["supplier"], "Beta")
        self.assertEqual(result["result"]["total"], "11500")
        self.assertEqual(result["budget"]["work_units_used"], 4)
        self.assertEqual([event["stage"] for event in result["trace"]], ["classification", "routing", "execution"])
        self.assertTrue(result["trace"][-1]["completion_evidence"]["supplier_verification_passed"])

    def test_existing_supplier_recovery_fixtures_remain_supported(self):
        for filename, fault, status, total in (
            ("supplier_task.json", "extract_timeout", "completed", "11500"),
            ("supplier_task.json", "extract_timeout_persistent", "retry_limit", None),
            ("supplier_task.json", "bad_arithmetic", "completed", "11500"),
            ("missing_shipping_control.json", "none", "needs_clarification", None),
            ("shipping_alias_control.json", "none", "completed", "10500"),
            ("csv_task.json", "none", "completed", "11500")):
            with self.subTest(filename=filename, fault=fault), patch(HTTP) as send:
                result = execute_request("Compare suppliers", registry(), supplier_task(filename),
                                         Budget(max_model_calls=0), fault=fault, offline=True)
            send.assert_not_called()
            self.assertEqual(result["status"], status)
            self.assertEqual((result["result"] or {}).get("total"), total)

    def test_supplier_admission_keeps_low_budget_clarification_and_retry_boundaries(self):
        missing = execute_request("Compare suppliers", registry(), supplier_task("missing_shipping_control.json"),
                                  Budget(max_units=3, max_model_calls=0), offline=True)
        self.assertEqual(missing["status"], "needs_clarification")
        self.assertEqual(missing["budget"]["work_units_used"], 3)
        persistent = execute_request("Compare suppliers", registry(), supplier_task(),
                                     Budget(max_units=2, max_model_calls=0), fault="extract_timeout_persistent", offline=True)
        self.assertEqual(persistent["status"], "retry_limit")
        self.assertEqual(persistent["budget"]["work_units_used"], 2)

    def test_ai_classification_and_extraction_share_one_cap_and_ledger(self):
        task = supplier_task()
        for cap, expected, calls in ((1, "budget_exhausted", 1), (2, "completed", 2)):
            bodies = [reply(content=classification()), reply(content=quote_response(task))]
            with self.subTest(cap=cap), patch(HTTP, side_effect=bodies) as send:
                result = execute_request("Compare suppliers", registry(), task, Budget(max_model_calls=cap),
                                         classifier_mode="ai", worker_model="local-worker")
            self.assertEqual(result["status"], expected)
            self.assertEqual(send.call_count, calls)
            self.assertEqual(result["budget"]["model_calls_used"], calls)
            self.assertEqual(len(result["model_usage"]), calls)
            self.assertEqual(result["model_usage"][0]["role"], "classifier")
            if calls == 2:
                self.assertEqual(result["model_usage"][1]["role"], "extraction_worker")
                self.assertEqual(result["tokens"]["prompt"], 24)
                self.assertTrue(result["supplier_execution"]["trace"][2]["verification_outcome"]["arithmetic_and_feasibility"])

    def test_broad_analysis_classification_can_route_a_bounded_supplier_intent(self):
        with patch(HTTP, return_value=reply(content=classification(caps=["analysis"]))) as send:
            result = execute_request("Compare suppliers", registry(), supplier_task(),
                                     Budget(max_model_calls=1), classifier_mode="ai")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["budget"]["model_calls_used"], 1)
        self.assertEqual(result["budget"]["work_units_used"], 5)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["trace"][1]["context_rule"], "bounded_supplier_intent_with_attachment")
        with patch(HTTP, return_value=reply(content=classification(caps=["analysis"]))):
            other = execute_request("Explain risks in the supplier data", registry(), supplier_task(),
                                    Budget(max_model_calls=1), classifier_mode="ai")
        self.assertNotEqual(other["status"], "completed")

    def test_openai_and_anthropic_extractors_use_existing_source_gate(self):
        for provider, variable, identifier in (("openai", "CORTEX_OPENAI_MODEL", "openai-configured"),
                                              ("anthropic", "CORTEX_ANTHROPIC_MODEL", "anthropic-configured")):
            task = supplier_task()
            env = {variable: "configured-test-model", "OPENAI_API_KEY": "fake-key", "ANTHROPIC_API_KEY": "fake-key"}
            with self.subTest(provider=provider), patch.dict(os.environ, env), \
                    patch(HTTP, return_value=reply(provider, quote_response(task))) as send:
                result = execute_request("Compare suppliers", registry(), task, Budget(max_model_calls=1), worker_model=identifier)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(result["supplier_execution"]["extraction_reports"][0]["provider"], provider)

    def test_provider_failure_never_retries_or_replaces_worker(self):
        with patch(HTTP, side_effect=TimeoutError("Missing shipping")) as send:
            result = execute_request("Compare suppliers", registry(), supplier_task(), worker_model="local-worker")
        self.assertEqual(result["status"], "worker_failed")
        self.assertEqual(send.call_count, 1)
        self.assertIsNone(result["tokens"]["prompt"])
        self.assertEqual(result["model_usage"][0]["error"]["category"], "timeout")
        self.assertNotIn("retry_extract", [e["action"] for e in result["supplier_execution"]["trace"]])

    def test_text_is_a_draft_not_claimed_execution_or_verification(self):
        bodies = [reply(content=classification("coding", ["code_generation"])), reply(content="def add(a, b): return a + b")]
        with patch(HTTP, side_effect=bodies):
            result = execute_request("Draft a Python add function", registry(), budget=Budget(max_model_calls=2))
        self.assertEqual(result["status"], "drafted")
        evidence = result["trace"][-1]["completion_evidence"]
        self.assertFalse(evidence["semantic_correctness_verified"])
        self.assertFalse(evidence["external_actions_performed"])

    def test_budget_and_override_fail_before_worker_execution(self):
        with patch(HTTP) as send:
            low = execute_request("Compare suppliers", registry(), supplier_task(), Budget(max_units=3))
            override = execute_request("Compare suppliers", registry(), supplier_task(), worker_model="local-classifier")
        send.assert_not_called()
        self.assertEqual(low["status"], "budget_exhausted")
        self.assertEqual(override["status"], "no_eligible_worker")
        with self.assertRaises(ValueError):
            execute_request("Compare suppliers", registry(), supplier_task(), worker_model="unregistered")

    def test_capability_failure_is_not_misdiagnosed_as_budget_exhaustion(self):
        proposal = classification(caps=["supplier_analysis", "github"])
        with patch(HTTP, return_value=reply(content=proposal)) as send:
            result = execute_request("Compare suppliers and update GitHub", registry(), supplier_task(),
                                     Budget(max_model_calls=1), classifier_mode="ai")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["status"], "no_eligible_worker")

    def test_classifier_cannot_spend_a_call_when_work_budget_is_empty(self):
        with patch(HTTP) as send:
            result = execute_request("Draft a Python function", registry(), budget=Budget(max_units=0))
        send.assert_not_called()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["model_calls_used"], 0)

    def test_cli_fresh_output_and_environment_secrets_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "new.json"
            args = ["Compare suppliers", "--task", str(ROOT / "examples/supplier_task.json"), "--offline", "--output", str(output)]
            with patch.dict(os.environ, {"OPENAI_API_KEY": "SECRET-KEY"}), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 0)
            old = output.read_bytes()
            self.assertNotIn(b"SECRET-KEY", old)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                main(args)
            self.assertEqual(output.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
