"""Failure-aware recovery contracts, using deterministic workers and mocked HTTP."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from cortex.cli import main
from cortex.domain import DataError, MissingFieldsError, ParsingError, Task, parse_source
from cortex.engine import run
from cortex.policies import Budget, LocalPolicy, OllamaPolicy


ROOT = Path(__file__).resolve().parents[1]
MODE = "failure-aware"


def task(name="supplier_task.json"):
    return Task.from_dict(json.loads((ROOT / "examples" / name).read_text()))


def response(action):
    return io.BytesIO(json.dumps({
        "message": {"content": json.dumps({"action": action, "reason": "Mock decision"})},
        "prompt_eval_count": 20,
        "eval_count": 4,
    }).encode())


def actions(result):
    return [event["action"] for event in result["trace"]]


class FailureAwareRecoveryTests(unittest.TestCase):
    def test_normal_path_keeps_verified_completion_and_zero_model_usage(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0), recovery_mode=MODE)
        send.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["supplier"], "Beta")
        self.assertEqual(result["result"]["total"], "11500")
        self.assertEqual(result["budget"]["work_units_used"], 4)
        self.assertEqual(result["budget"]["model_calls_used"], 0)
        self.assertEqual(result["tokens"]["prompt"], 0)
        self.assertEqual(result["tokens"]["output"], 0)
        self.assertEqual(actions(result), ["extract", "calculate", "verify", "recommend"])
        self.assertTrue(all(event["decision_source"] == "rule" for event in result["trace"]))

    def test_one_time_extraction_timeout_recovers_without_model_or_planner(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0), fault="extract_timeout",
                         recovery_mode=MODE)
        send.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["supplier"], "Beta")
        self.assertEqual(result["result"]["total"], "11500")
        self.assertEqual(result["budget"]["work_units_used"], 5)
        self.assertEqual(result["budget"]["model_calls_used"], 0)
        self.assertEqual(actions(result),
                         ["extract", "retry_extract", "calculate", "verify", "recommend"])
        failure, retry = result["trace"][:2]
        self.assertEqual(failure["failure_category"], "transient_execution")
        self.assertEqual(retry["decision_failure_category"], "transient_execution")
        self.assertEqual(retry["decision_source"], "rule")
        self.assertEqual(retry["extraction_retry_number"], 1)

    def test_persistent_timeout_terminates_after_one_retry(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0), fault="extract_timeout_persistent",
                         recovery_mode=MODE)
        send.assert_not_called()
        self.assertEqual(result["status"], "retry_limit")
        self.assertIsNone(result["result"])
        self.assertEqual(result["budget"]["work_units_used"], 2)
        self.assertEqual(actions(result), ["extract", "retry_extract", "stop"])
        self.assertEqual(actions(result).count("retry_extract"), 1)
        self.assertTrue(all(event["failure_category"] == "transient_execution"
                            for event in result["trace"]))
        self.assertTrue(all(event["decision_source"] == "rule"
                            for event in result["trace"]))

    def test_true_missing_shipping_clarifies_only_after_alias_handling(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task("missing_shipping_control.json"),
                         OllamaPolicy("test", selective=True), Budget(max_model_calls=0),
                         recovery_mode=MODE)
        send.assert_not_called()
        self.assertEqual(result["status"], "needs_clarification")
        self.assertIsNone(result["result"])
        self.assertEqual(result["budget"]["work_units_used"], 3)
        self.assertEqual(actions(result), ["extract", "repair_extract", "clarify"])
        first, repaired, clarification = result["trace"]
        self.assertEqual(first["failure_category"], "parsing_problem")
        self.assertFalse(first["aliases_checked"])
        self.assertEqual(repaired["failure_category"], "missing_information")
        self.assertTrue(repaired["aliases_checked"])
        self.assertEqual(repaired["missing_fields"], ["shipping"])
        self.assertEqual(clarification["decision_failure_category"], "missing_information")

    def test_freight_alias_remains_recoverable_without_model(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task("shipping_alias_control.json"),
                         OllamaPolicy("test", selective=True), Budget(max_model_calls=0),
                         recovery_mode=MODE)
        send.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["supplier"], "Alpha")
        self.assertEqual(result["result"]["total"], "10500")
        self.assertEqual(result["budget"]["work_units_used"], 6)
        self.assertEqual(actions(result).count("repair_extract"), 1)
        self.assertNotIn("retry_extract", actions(result))
        self.assertNotIn("clarify", actions(result))
        self.assertEqual(result["trace"][1]["decision_failure_category"], "parsing_problem")

    def test_supported_csv_format_uses_flexible_repair(self):
        result = run(task("csv_task.json"), budget=Budget(max_model_calls=0),
                     recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["supplier"], "Beta")
        self.assertEqual(result["budget"]["work_units_used"], 6)
        self.assertEqual(result["trace"][0]["failure_category"], "parsing_problem")
        self.assertNotIn("retry_extract", actions(result))
        self.assertNotIn("plan", actions(result))

    def test_insufficient_work_budget_blocks_retry_before_execution(self):
        with patch("cortex.engine.parse_source", side_effect=TimeoutError("worker busy")) as extract:
            result = run(task(), budget=Budget(max_units=1, max_model_calls=0),
                         recovery_mode=MODE)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["work_units_used"], 1)
        self.assertEqual(actions(result), ["extract", "stop"])
        self.assertEqual(result["trace"][-1]["decision_failure_category"], "transient_execution")

    def test_timeout_classification_uses_type_even_when_message_mentions_missing_shipping(self):
        calls = []

        def extract(source, robust=False):
            calls.append(robust)
            if len(calls) == 1:
                raise TimeoutError("Missing quote fields: shipping; aliases unavailable")
            return parse_source(source, robust=robust)

        with patch("cortex.engine.parse_source", side_effect=extract):
            result = run(task(), budget=Budget(max_model_calls=0), recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, [False, False])
        self.assertEqual(result["trace"][0]["failure_category"], "transient_execution")
        self.assertNotIn("clarify", actions(result))

    def test_timeout_words_in_data_error_do_not_trigger_execution_retry(self):
        calls = 0

        def extract(source, robust=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DataError("Extraction timeout: try again")
            return parse_source(source, robust=robust)

        with patch("cortex.engine.parse_source", side_effect=extract):
            result = run(task(), recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["trace"][0]["failure_category"], "unclassified")
        self.assertIn("plan", actions(result))
        self.assertNotIn("retry_extract", actions(result))

    def test_parsing_error_with_timeout_message_uses_flexible_parser(self):
        calls = []

        def extract(source, robust=False):
            calls.append(robust)
            if len(calls) == 1:
                raise ParsingError("timeout")
            return parse_source(source, robust=robust)

        with patch("cortex.engine.parse_source", side_effect=extract):
            result = run(task(), recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, [False, True])
        self.assertEqual(result["trace"][0]["failure_category"], "parsing_problem")
        self.assertNotIn("retry_extract", actions(result))

    def test_retry_preserves_strict_parsing_then_repairs_alias_separately(self):
        with patch("cortex.engine.parse_source", wraps=parse_source) as extract:
            result = run(task("shipping_alias_control.json"), fault="extract_timeout",
                         recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["total"], "10500")
        self.assertEqual(result["budget"]["work_units_used"], 7)
        self.assertEqual([call.kwargs["robust"] for call in extract.call_args_list], [False, True])
        self.assertEqual(actions(result),
                         ["extract", "retry_extract", "repair_extract", "calculate", "verify", "recommend"])

    def test_retry_allowance_cannot_be_reset_by_format_repair(self):
        failures = [TimeoutError("first"), MissingFieldsError(["shipping"]),
                    TimeoutError("repair also timed out")]
        with patch("cortex.engine.parse_source", side_effect=failures) as extract:
            result = run(task(), recovery_mode=MODE)
        self.assertEqual(extract.call_count, 3)
        self.assertEqual(result["status"], "retry_limit")
        self.assertEqual(actions(result), ["extract", "retry_extract", "repair_extract", "stop"])
        self.assertEqual(actions(result).count("retry_extract"), 1)
        self.assertEqual(result["budget"]["work_units_used"], 4)

    def test_retry_allowance_is_per_run_when_policy_is_reused(self):
        policy = LocalPolicy()
        for _ in range(2):
            result = run(task(), policy, fault="extract_timeout", recovery_mode=MODE)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(actions(result).count("retry_extract"), 1)

    def test_timeout_during_flexible_repair_retries_that_parser_setting(self):
        calls = []

        def extract(source, robust=False):
            calls.append(robust)
            if len(calls) == 2:
                raise TimeoutError("worker unavailable")
            return parse_source(source, robust=robust)

        with patch("cortex.engine.parse_source", side_effect=extract):
            result = run(task("shipping_alias_control.json"), recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["total"], "10500")
        self.assertEqual(calls, [False, True, True])
        self.assertEqual(actions(result).count("retry_extract"), 1)

    def test_local_planner_cannot_repeat_an_exhausted_flexible_repair(self):
        with patch("cortex.engine.parse_source", side_effect=[
                MissingFieldsError(["shipping"]), DataError("unsupported values")]) as extract:
            result = run(task(), recovery_mode=MODE)
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(actions(result), ["extract", "repair_extract", "plan", "clarify"])
        self.assertEqual(actions(result).count("repair_extract"), 1)

    def test_arithmetic_error_is_verified_and_recalculated_once(self):
        with patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0), fault="bad_arithmetic", recovery_mode=MODE)
        send.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["supplier"], "Beta")
        self.assertEqual(result["result"]["total"], "11500")
        self.assertEqual(result["budget"]["work_units_used"], 7)
        self.assertEqual(actions(result).count("repair_calculation"), 1)
        self.assertEqual(actions(result).count("verify"), 2)
        failed_verify = next(event for event in result["trace"]
                             if event["action"] == "verify" and event["status"] == "failed")
        self.assertEqual(failed_verify["failure_category"], "arithmetic_integrity")
        repair = next(event for event in result["trace"] if event["action"] == "repair_calculation")
        self.assertEqual(repair["decision_failure_category"], "arithmetic_integrity")

    def test_persistent_arithmetic_failure_terminates_after_bounded_recalculation(self):
        with patch("cortex.engine.verify", return_value="calculation_mismatch") as verify:
            result = run(task(), recovery_mode=MODE)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result["status"], "recovery_limit")
        self.assertEqual(actions(result).count("repair_calculation"), 1)
        self.assertNotIn("recommend", actions(result))
        self.assertNotIn("plan", actions(result))
        self.assertEqual(result["budget"]["work_units_used"], 6)

    def test_source_integrity_failure_reextracts_then_verifies(self):
        result = run(task(), fault="corrupt_extraction", recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["supplier"], "Beta")
        self.assertEqual(result["budget"]["work_units_used"], 8)
        failed_verify = next(event for event in result["trace"]
                             if event["action"] == "verify" and event["status"] == "failed")
        self.assertEqual(failed_verify["failure_category"], "source_integrity")
        self.assertEqual(actions(result).count("repair_extract"), 1)
        self.assertEqual(actions(result).count("verify"), 2)

    def test_work_budget_sweep_never_overruns_or_skips_verification(self):
        for cap in range(10):
            for fault in ("none", "extract_timeout", "extract_timeout_persistent",
                          "bad_arithmetic", "corrupt_extraction"):
                with self.subTest(cap=cap, fault=fault):
                    result = run(task(), budget=Budget(max_units=cap, max_model_calls=0),
                                 fault=fault, recovery_mode=MODE)
                    self.assertLessEqual(result["budget"]["work_units_used"], cap)
                    self.assertLessEqual(actions(result).count("retry_extract"), 1)
                    if result["status"] == "completed":
                        self.assertEqual(result["result"]["supplier"], "Beta")
                        last_verify = next(event for event in reversed(result["trace"])
                                           if event["action"] == "verify")
                        self.assertEqual(last_verify["status"], "ok")


class RecoveryPlannerAndProviderTests(unittest.TestCase):
    def unclassified_then_valid(self):
        calls = 0

        def extract(source, robust=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise DataError("Unsupported source issue")
            return parse_source(source, robust=robust)

        return extract

    def test_unclassified_failure_retains_model_planning_with_shared_call_cap(self):
        with patch("cortex.engine.parse_source", side_effect=self.unclassified_then_valid()), \
                patch("urllib.request.urlopen", return_value=response("repair_extract")) as send:
            result = run(task(), OllamaPolicy("small", "strong", selective=True),
                         Budget(max_model_calls=1), recovery_mode=MODE)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["budget"]["model_calls_used"], 1)
        self.assertEqual(result["tokens"]["prompt"], 20)
        self.assertEqual(result["tokens"]["output"], 4)
        self.assertEqual(result["model_usage"][0]["model"], "strong")
        self.assertEqual(result["model_usage"][0]["role"], "planner")
        planner = next(event for event in result["trace"] if event["action"] == "plan")
        self.assertEqual(planner["decision_source"], "rule")
        self.assertEqual(planner["decision_failure_category"], "unclassified")
        self.assertEqual(planner["planner_decision_source"], "model")
        payload = json.loads(send.call_args.args[0].data)
        state = json.loads(payload["messages"][1]["content"])
        self.assertEqual(state["admissible_actions"], ["repair_extract", "clarify"])
        self.assertEqual(state["failure_category"], "unclassified")
        self.assertEqual(state["remaining_model_calls"], 1)

    def test_zero_call_cap_blocks_unknown_planning_without_provider_or_fallback(self):
        with patch("cortex.engine.parse_source", side_effect=DataError("unknown")) as extract, \
                patch("urllib.request.urlopen") as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=0), recovery_mode=MODE)
        send.assert_not_called()
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["budget"]["model_calls_used"], 0)
        self.assertNotIn("repair_extract", actions(result))
        self.assertEqual(result["tokens"]["prompt"], 0)

    def test_planner_http_timeout_is_counted_failure_without_retry_or_fallback(self):
        with patch("cortex.engine.parse_source", side_effect=DataError("worker source issue")) as extract, \
                patch("urllib.request.urlopen", side_effect=TimeoutError("provider offline")) as send:
            result = run(task(), OllamaPolicy("test", selective=True),
                         Budget(max_model_calls=3), recovery_mode=MODE)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(result["status"], "controller_failed")
        self.assertEqual(result["budget"]["model_calls_used"], 1)
        self.assertIsNone(result["tokens"]["prompt"])
        self.assertIsNone(result["tokens"]["output"])
        self.assertFalse(result["tokens"]["usage_complete"])
        self.assertNotIn("retry_extract", actions(result))
        self.assertNotIn("repair_extract", actions(result))
        planner = result["trace"][-1]
        self.assertEqual(planner["failure_category"], "controller_failure")
        self.assertEqual(planner["planner_decision_source"], "model")

    def test_baseline_retains_original_timeout_choice_and_model_state(self):
        for kwargs in ({}, {"recovery_mode": "baseline"}):
            with self.subTest(kwargs=kwargs), patch("urllib.request.urlopen", return_value=response("repair_extract")) as send:
                result = run(task(), OllamaPolicy("test", selective=True),
                             Budget(max_model_calls=1), fault="extract_timeout", **kwargs)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(result["budget"]["work_units_used"], 6)
            self.assertNotIn("retry_extract", actions(result))
            payload = json.loads(send.call_args.args[0].data)
            state = json.loads(payload["messages"][1]["content"])
            self.assertEqual(state["admissible_actions"], ["repair_extract", "plan", "stop"])
            self.assertNotIn("failure_category", state)
            self.assertNotIn("recovery_mode", state)
            self.assertNotIn("extraction_retries", state)


class RecoveryCliTests(unittest.TestCase):
    def test_local_cli_enables_recovery_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "new_recovery_run.json"
            args = [str(ROOT / "examples" / "supplier_task.json"), "--policy", "local",
                    "--recovery-mode", MODE, "--fault", "extract_timeout",
                    "--max-model-calls", "0", "--output", str(output)]
            with contextlib.redirect_stdout(io.StringIO()), patch("urllib.request.urlopen") as send:
                self.assertEqual(main(args), 0)
            send.assert_not_called()
            saved = output.read_bytes()
            result = json.loads(saved)
            self.assertEqual(result["configuration"]["recovery_mode"], MODE)
            self.assertEqual(result["budget"]["work_units_used"], 5)
            self.assertIn("engine.py", result["experiment"]["source_sha256"])
            with contextlib.redirect_stderr(io.StringIO()), patch("urllib.request.urlopen") as send:
                with self.assertRaises(SystemExit):
                    main(args)
            send.assert_not_called()
            self.assertEqual(output.read_bytes(), saved)

    def test_selective_ollama_cli_runs_retry_with_zero_call_budget(self):
        args = [str(ROOT / "examples" / "supplier_task.json"), "--policy", "ollama",
                "--model", "test", "--selective", "--recovery-mode", MODE,
                "--fault", "extract_timeout", "--max-model-calls", "0"]
        rendered = io.StringIO()
        with contextlib.redirect_stdout(rendered), patch("urllib.request.urlopen") as send:
            self.assertEqual(main(args), 0)
        send.assert_not_called()
        result = json.loads(rendered.getvalue())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["budget"]["model_calls_used"], 0)

    def test_recovery_cli_rejects_fixed_jev_and_nonselective_ollama(self):
        for policy in ("fixed", "jev", "ollama"):
            args = [str(ROOT / "examples" / "supplier_task.json"), "--policy", policy,
                    "--model", "test", "--recovery-mode", MODE]
            with self.subTest(policy=policy), contextlib.redirect_stderr(io.StringIO()), \
                    patch("urllib.request.urlopen") as send:
                with self.assertRaises(SystemExit) as error:
                    main(args)
            self.assertEqual(error.exception.code, 2)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
