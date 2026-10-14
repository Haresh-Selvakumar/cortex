"""Selective governor behavior, tested at the real HTTP adapter boundary."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex.cli import CODE_CHECKPOINT, main
from cortex.domain import Task
from cortex.engine import run
from cortex.policies import Budget, JevPolicy, OllamaPolicy


ROOT = Path(__file__).resolve().parents[1]


def task(name="supplier_task.json"):
    return Task.from_dict(json.loads((ROOT / "examples" / name).read_text()))


def response(action, prompt_tokens=20, output_tokens=4):
    raw = {"message": {"content": json.dumps({"action": action, "reason": "Mock decision"})},
           "prompt_eval_count": prompt_tokens, "eval_count": output_tokens}
    return io.BytesIO(json.dumps(raw).encode())


class SelectiveTests(unittest.TestCase):
    def test_normal_path_has_no_http_or_tokens_with_zero_call_budget(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0))
        send.assert_not_called()
        self.assertEqual(result["result"]["supplier"], "Beta")
        self.assertEqual(result["result"]["total"], "11500")
        self.assertEqual(result["budget"]["work_units_used"], 4)
        self.assertEqual(result["budget"]["model_calls_used"], 0)
        self.assertEqual(result["tokens"]["prompt"], 0)
        self.assertEqual(result["tokens"]["output"], 0)
        self.assertEqual([e["action"] for e in result["trace"]],
                         ["extract", "calculate", "verify", "recommend"])
        self.assertTrue(all(e["decision_source"] == "rule" for e in result["trace"]))

    def test_ordinary_mode_still_calls_model_for_every_decision(self):
        replies = [response(a) for a in ("extract", "calculate", "verify", "recommend")]
        with patch("urllib.request.urlopen", side_effect=replies) as send:
            result = run(task(), OllamaPolicy("test"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(send.call_count, 4)
        self.assertEqual(result["budget"]["model_calls_used"], 4)
        self.assertEqual(result["tokens"]["prompt"], 80)

    def test_ambiguous_recovery_invokes_model_once_then_finishes_by_rules(self):
        with patch("urllib.request.urlopen", return_value=response("repair_extract")) as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=1), fault="extract_timeout")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["budget"]["model_calls_used"], 1)
        payload = json.loads(send.call_args.args[0].data)
        state = json.loads(payload["messages"][1]["content"])
        self.assertEqual(state["admissible_actions"], ["repair_extract", "plan", "stop"])
        self.assertIn("Injected extraction timeout", state["error_detail"])
        self.assertEqual(state["remaining_model_calls"], 1)

    def test_ambiguous_recovery_with_zero_calls_stops_without_fallback(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0), fault="extract_timeout")
        send.assert_not_called()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["model_calls_used"], 0)
        self.assertEqual(len(result["trace"]), 1)

    def test_call_cap_applies_to_planner_and_controller_together(self):
        for cap, expected_calls, status in ((1, 1, "budget_exhausted"), (2, 2, "completed")):
            with self.subTest(cap=cap), patch("urllib.request.urlopen", side_effect=[
                    response("plan"), response("repair_extract")]) as send:
                result = run(task(), OllamaPolicy("small", "strong", selective=True),
                             Budget(max_model_calls=cap), fault="extract_timeout")
            self.assertEqual(result["status"], status)
            self.assertEqual(send.call_count, expected_calls)
            self.assertEqual(result["budget"]["model_calls_used"], expected_calls)
            self.assertEqual(len(result["model_usage"]), expected_calls)
            if cap == 2:
                self.assertEqual([r["model"] for r in result["model_usage"]], ["small", "strong"])
                self.assertEqual([r["role"] for r in result["model_usage"]], ["controller", "planner"])
                plan_event = next(e for e in result["trace"] if e["action"] == "plan")
                self.assertEqual(plan_event["planner_action"], "repair_extract")
                planner_payload = json.loads(send.call_args.args[0].data)
                state = json.loads(planner_payload["messages"][1]["content"])
                self.assertEqual(state["admissible_actions"], ["repair_extract", "clarify"])
                self.assertEqual(state["remaining_model_calls"], 1)
                self.assertEqual(state["remaining_units"], 16)

    def test_model_can_choose_stop_at_ambiguous_decision(self):
        with patch("urllib.request.urlopen", return_value=response("stop")):
            result = run(task(), OllamaPolicy("test", selective=True), fault="extract_timeout")
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["budget"]["model_calls_used"], 1)

    def test_provider_failures_count_attempts_and_stop_in_both_roles(self):
        for replies, expected_calls in (([TimeoutError("offline")], 1),
                                        ([response("plan"), ValueError("invalid JSON")], 2)):
            with self.subTest(calls=expected_calls), patch("urllib.request.urlopen", side_effect=replies) as send:
                result = run(task(), OllamaPolicy("test", selective=True), fault="extract_timeout")
            self.assertEqual(result["status"], "controller_failed")
            self.assertEqual(send.call_count, expected_calls)
            self.assertEqual(result["budget"]["model_calls_used"], expected_calls)
            self.assertIsNone(result["tokens"]["prompt"])
            self.assertFalse(result["tokens"]["usage_complete"])
            self.assertNotIn("repair_extract", [e["action"] for e in result["trace"]])

    def test_invalid_planner_choice_stops_with_reported_tokens(self):
        with patch("urllib.request.urlopen", side_effect=[response("plan"), response("shell")]):
            result = run(task(), OllamaPolicy("test", selective=True), fault="extract_timeout")
        self.assertEqual(result["status"], "controller_failed")
        self.assertEqual(result["budget"]["model_calls_used"], 2)
        self.assertEqual(result["tokens"]["prompt"], 40)

    def test_work_filter_can_make_recovery_a_rule_decision(self):
        # After extraction, only repair (2 units) fits; planning (3) does not.
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_units=3, max_model_calls=0), fault="extract_timeout")
        send.assert_not_called()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["work_units_used"], 3)
        self.assertEqual([e["action"] for e in result["trace"]], ["extract", "repair_extract"])

    def test_planner_sole_affordable_option_is_also_selected_by_code(self):
        with patch("urllib.request.urlopen", return_value=response("plan")) as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_units=4, max_model_calls=1), fault="extract_timeout")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["status"], "needs_clarification")
        event = next(e for e in result["trace"] if e["action"] == "plan")
        self.assertEqual(event["planner_action"], "clarify")
        self.assertEqual(event["planner_decision_source"], "rule")

    def test_arithmetic_repair_needs_no_model(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0), fault="bad_arithmetic")
        send.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertIn("repair_calculation", [e["action"] for e in result["trace"]])

    def test_missing_shipping_clarifies_after_model_selects_flexible_parse(self):
        with patch("urllib.request.urlopen", return_value=response("repair_extract")) as send:
            result = run(task("missing_shipping.json"), OllamaPolicy("test", selective=True))
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["budget"]["work_units_used"], 3)
        self.assertEqual(send.call_count, 1)

    def test_missing_jev_key_is_zero_actual_attempts(self):
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.urlopen") as send:
            result = run(task(), JevPolicy())
        send.assert_not_called()
        self.assertEqual(result["status"], "controller_failed")
        self.assertEqual(result["budget"]["model_calls_used"], 0)


class SelectiveCliTests(unittest.TestCase):
    def test_selective_rejected_for_other_policies(self):
        for policy in ("local", "fixed", "jev"):
            with self.subTest(policy=policy), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main([str(ROOT / "examples" / "supplier_task.json"),
                          "--policy", policy, "--selective"])
                self.assertEqual(error.exception.code, 2)

    def test_cli_wires_selective_and_records_provenance_without_overwriting(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "run.json"
            args = [str(ROOT / "examples" / "supplier_task.json"), "--policy", "ollama",
                    "--model", "test", "--selective", "--max-model-calls", "0",
                    "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()), patch("urllib.request.urlopen") as send:
                self.assertEqual(main(args), 0)
            send.assert_not_called()
            saved = output.read_bytes()
            result = json.loads(saved)
            self.assertEqual(result["policy"], "ollama_selective")
            self.assertTrue(result["configuration"]["selective"])
            self.assertEqual(result["experiment"]["code_checkpoint"], CODE_CHECKPOINT)
            self.assertIn("policies.py", result["experiment"]["source_sha256"])
            with contextlib.redirect_stderr(io.StringIO()), patch("urllib.request.urlopen") as send:
                with self.assertRaises(SystemExit):
                    main(args)
            send.assert_not_called()
            self.assertEqual(output.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
