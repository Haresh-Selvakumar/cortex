"""Deterministic checks, mocked inference and actual SQLite/OS lock tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from cortex.draft_checks import DraftPolicy, check_draft
from cortex.job_store import JobStore, JobError
from cortex.job_session import JobSession
from cortex.jobs import new_job, resume_job
from cortex.product import execute_request
from cortex.registry import Registry
from cortex.policies import Budget

ROOT = Path(__file__).resolve().parents[1]
HTTP = "urllib.request.urlopen"


def registry_data():
    return json.loads((ROOT / "config/cortex.example.json").read_text())


def task_data():
    return json.loads((ROOT / "examples/supplier_task.json").read_text())


def plan(independent=False, supplier=False):
    steps = [{"id": "first", "task_type": "analysis" if supplier else "documents", "goal": "Compare suppliers" if supplier else "Draft a brief introduction.",
              "execution_kind": "supplier" if supplier else "text_draft",
              "required_inputs": ["user_goal", "supplier_task"] if supplier else ["user_goal"],
              "missing_essential_information": [], "depends_on": [],
              "required_capabilities": ["analysis", "supplier_analysis"] if supplier else ["document_draft"],
              "expected_output": "supplier_recommendation" if supplier else "text_draft",
              "completion_checks": ["supplier_verified"] if supplier else ["text_present"]}]
    if independent or supplier:
        steps.append({"id": "second", "task_type": "documents", "goal": "Draft a short summary.", "execution_kind": "text_draft",
                      "required_inputs": ["user_goal"] + ([] if independent else ["predecessor_outputs"]),
                      "missing_essential_information": [], "depends_on": [] if independent else ["first"],
                      "required_capabilities": ["document_draft"], "expected_output": "text_draft", "completion_checks": ["text_present"]})
    return {"version": 1, "steps": steps}


def response(content):
    return io.BytesIO(json.dumps({"model": "mock-model", "done": True, "done_reason": "stop",
        "message": {"content": content if type(content) is str else json.dumps(content)},
        "prompt_eval_count": 10, "eval_count": 5}).encode())


class DraftCheckTests(unittest.TestCase):
    def test_word_count_rule_includes_code_and_punctuation_tokens(self):
        report = check_draft("one-two\tthree\n```\n!", DraftPolicy.validate({"max_words": 3}))
        self.assertFalse(report["passed"])
        self.assertEqual(report["checked_constraints"][0]["observed"], 4)

    def test_required_sections_are_standalone_headings(self):
        policy = DraftPolicy.validate({"required_sections": ["Recommendation", "Limits"]})
        self.assertTrue(check_draft("# Recommendation\nBeta.\nLimits:\nUnverified prose.", policy)["passed"])
        self.assertFalse(check_draft("The Recommendation is Beta and Limits apply.", policy)["passed"])

    def test_unspecified_currency_rejects_symbols_codes_and_known_names(self):
        policy = DraftPolicy.validate()
        for text in ("$11500", "USD 11500", "11,500 dollars", "₹11500", "€11500", "₿1"):
            with self.subTest(text=text):
                self.assertFalse(check_draft(text, policy)["passed"])
        self.assertTrue(check_draft("Beta total 11500; currency unspecified.", policy)["passed"])

    def test_authoritative_currency_only_allows_matching_labels(self):
        policy = DraftPolicy.validate(currency="USD")
        self.assertTrue(check_draft("USD $11500 dollars", policy)["passed"])
        self.assertFalse(check_draft("EUR €11500", policy)["passed"])

    def test_recorded_bad_supplier_recommendations_are_now_rejected(self):
        # Public regression inputs exercise the same invented-currency rejection;
        # original full live traces remain private in the frozen checkpoint.
        fixtures = json.loads((ROOT / "tests" / "fixtures" / "currency_rejections.json").read_text(encoding="utf-8"))
        for text in fixtures["drafts"]:
            self.assertFalse(check_draft(text, DraftPolicy.validate())["passed"])

    def test_policy_rejects_arbitrary_checks_untrusted_currency_and_excess_repairs(self):
        for settings in ({"max_words": True}, {"max_words": 0}, {"required_sections": ["A", "a"]},
                         {"required_sections": ["two\nlines"]}, {"allowed_currencies": ["USD"]}, {"semantic_correctness": True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                DraftPolicy.validate(settings)
        for limit in (-1, 3, True):
            with self.assertRaises(ValueError):
                DraftPolicy.validate(max_repairs=limit)

    def test_unchecked_prose_and_factual_limit_stay_visible(self):
        report = check_draft("Unverified text", DraftPolicy.validate(), ["Be persuasive"])
        self.assertTrue(report["passed"])
        self.assertEqual(report["unchecked_constraints"], ["Be persuasive"])
        self.assertEqual(report["factual_verification"], "not_performed")
        self.assertFalse(report["model_confidence_used"])


class RefinementTests(unittest.TestCase):
    def execute(self, replies, policy, budget=None, payload=None):
        with patch(HTTP, side_effect=replies) as http:
            result = execute_request("Draft", Registry(registry_data()), budget=budget, plan_payload=payload or plan(),
                                     draft_policy=policy, constraints=["Be persuasive"])
        return result, http

    def test_word_limit_failure_can_be_repaired_once(self):
        result, http = self.execute([response("one two three four"), response("one two")], DraftPolicy.validate({"max_words": 2}))
        self.assertEqual(result["status"], "drafted")
        row = result["step_results"][0]
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(len(row["refinement_history"]), 2)
        self.assertEqual([x["role"] for x in result["model_usage"]], ["worker", "draft_repair"])
        self.assertEqual(result["budget"]["model_calls_used"], 2)
        wire = json.loads(http.call_args_list[1].args[0].data)
        data = json.loads(wire["messages"][1]["content"])
        self.assertEqual(data["REPAIR_DATA"]["previous_rejected_draft"], "one two three four")
        self.assertEqual(data["REPAIR_DATA"]["specific_check_failures"][0]["check"], "max_words")
        self.assertEqual(row["draft_check_report"]["unchecked_constraints"], ["Be persuasive"])

    def test_rejected_currency_is_repaired_and_never_passed_to_dependency(self):
        payload = plan(supplier=True)
        from cortex.domain import Task
        with patch(HTTP, side_effect=[response("Beta $11500"), response("Beta 11500, currency unspecified")]) as http:
            result = execute_request("Compare then draft", Registry(registry_data()), Task.from_dict(task_data()),
                                     plan_payload=payload, draft_policy=DraftPolicy.validate())
        self.assertEqual(http.call_count, 2)
        row = result["step_results"][1]
        self.assertEqual(row["output"]["text"], "Beta 11500, currency unspecified")
        self.assertTrue(result["step_results"][0]["completion_evidence"]["supplier_verification_passed"])

    def test_failed_refinement_blocks_dependents_and_keeps_rejected_output_out(self):
        payload = plan(independent=True)
        payload["steps"][1].update(depends_on=["first"], required_inputs=["user_goal", "predecessor_outputs"])
        result, http = self.execute([response("$1"), response("$2")], DraftPolicy.validate(), payload=payload)
        self.assertEqual(http.call_count, 2)
        self.assertEqual([row["status"] for row in result["step_results"]], ["refinement_failed", "blocked_dependency"])
        self.assertIsNone(result["step_results"][0]["output"])

    def test_repeated_failed_draft_stops_without_using_remaining_repair(self):
        result, http = self.execute([response("$1"), response("$1")], DraftPolicy.validate(max_repairs=2))
        self.assertEqual(http.call_count, 2)
        self.assertEqual(result["step_results"][0]["refinement_stop"], "repeated_without_progress")

    def test_call_or_work_budget_blocks_repair_before_spending(self):
        for budget in (Budget(max_model_calls=1), Budget(max_units=1)):
            result, http = self.execute([response("$1")], DraftPolicy.validate(), budget)
            self.assertEqual(http.call_count, 1)
            self.assertEqual(result["status"], "budget_exhausted")
            self.assertEqual(result["budget"]["work_units_used"], 1)

    def test_known_provider_failure_is_not_refined_or_retried(self):
        result, http = self.execute([TimeoutError("words are irrelevant")], DraftPolicy.validate())
        self.assertEqual(http.call_count, 1)
        self.assertEqual(result["step_results"][0]["status"], "worker_failed")
        self.assertEqual(result["model_usage"][0]["error"]["category"], "timeout")

    def test_explicit_zero_repair_limit_stops_rejected_generation(self):
        result, http = self.execute([response("$1")], DraftPolicy.validate(max_repairs=0))
        self.assertEqual(http.call_count, 1)
        self.assertEqual(result["step_results"][0]["status"], "refinement_failed")


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.folder.name) / "jobs.sqlite3")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def new(self, **kwargs):
        return new_job(self.store, "Produce drafts", registry_data(), plan=plan(independent=True), **kwargs)

    def test_checkpoint_resume_reuses_accepted_predecessor_and_budget(self):
        with patch(HTTP, return_value=response("First accepted")) as http:
            first = self.new(pause_after=1, max_model_calls=2)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(first["status"], "interrupted")
        saved = self.store.read(first["job_id"])
        self.assertEqual(saved["steps"]["first"]["state"], "accepted")
        with patch(HTTP, return_value=response("Second accepted")) as http:
            resumed = resume_job(self.store, first["job_id"])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(resumed["budget"]["model_calls_used"], 2)
        self.assertEqual(resumed["budget"]["work_units_used"], 2)
        with patch(HTTP) as http:
            same = resume_job(self.store, first["job_id"])
        http.assert_not_called()
        self.assertTrue(same["terminal_job_reused"])

    def test_interrupt_in_flight_retains_reserved_call_and_unknown_outcome(self):
        with patch(HTTP, side_effect=KeyboardInterrupt):
            first = new_job(self.store, "Draft", registry_data(), plan=plan(), max_model_calls=2)
        saved = self.store.read(first["job_id"])
        self.assertEqual(saved["attempts"][0]["status"], "unknown")
        self.assertEqual(saved["budget"]["model_calls"], 1)
        self.assertEqual(saved["steps"]["first"]["state"], "interrupted")
        with patch(HTTP) as http:
            blocked = resume_job(self.store, first["job_id"])
        http.assert_not_called()
        self.assertEqual(blocked["status"], "interrupted")
        with patch(HTTP, return_value=response("Replacement")) as http:
            resumed = resume_job(self.store, first["job_id"], retry_interrupted=True)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(resumed["budget"]["model_calls_used"], 2)
        self.assertEqual(resumed["budget"]["work_units_used"], 2)
        self.assertEqual(len(resumed["model_usage"]), 2)
        self.assertEqual(resumed["model_usage"][0]["status"], "interrupted_unknown")

    def test_unknown_attempt_also_counts_against_draft_attempt_limit(self):
        with patch(HTTP, side_effect=KeyboardInterrupt):
            first = new_job(self.store, "Draft", registry_data(), plan=plan(), max_model_calls=5)
        with patch(HTTP, return_value=response("$1")) as http:
            resumed = resume_job(self.store, first["job_id"], retry_interrupted=True)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(resumed["budget"]["model_calls_used"], 2)
        self.assertEqual(resumed["step_results"][0]["refinement_stop"], "repair_limit_including_unknown_attempts")

    def test_saved_response_replays_after_crash_before_acceptance_without_new_call(self):
        import cortex.refinement
        real = cortex.refinement.check_draft
        with patch(HTTP, return_value=response("Saved response")), patch("cortex.refinement.check_draft", side_effect=KeyboardInterrupt):
            first = new_job(self.store, "Draft", registry_data(), plan=plan(), max_model_calls=1, max_units=1)
        with patch(HTTP) as http:
            resumed = resume_job(self.store, first["job_id"])
        http.assert_not_called()
        self.assertEqual(resumed["status"], "drafted")
        self.assertEqual(resumed["budget"]["model_calls_used"], 1)
        self.assertEqual(resumed["budget"]["work_units_used"], 1)

    def test_classifier_response_is_durable_before_consumer_validation(self):
        import cortex.classifier
        original = cortex.classifier.Classification.validate
        proposal = {"task_type": "documents", "required_capabilities": ["document_draft"], "missing_essential_information": [],
                    "needs_decomposition": False, "uncertain": False}
        with patch(HTTP, return_value=response(proposal)), patch("cortex.classifier.Classification.validate", side_effect=KeyboardInterrupt):
            first = new_job(self.store, "Draft a paragraph", registry_data(), max_model_calls=2)
        with patch(HTTP, return_value=response("Draft response")) as http:
            resumed = resume_job(self.store, first["job_id"])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(resumed["status"], "drafted")
        self.assertEqual(resumed["budget"]["model_calls_used"], 2)
        self.assertEqual([row["role"] for row in resumed["model_usage"]], ["classifier", "worker"])

    def test_planner_response_replays_without_resetting_consumption(self):
        supplier = plan(supplier=True)
        supplier["steps"] = supplier["steps"][:1]
        from cortex.planning import Plan
        original = Plan.validate
        called = False
        def reply(*args, **kwargs):
            nonlocal called
            called = True
            return response(supplier)
        def validate(payload, *args, **kwargs):
            if called and args:
                raise KeyboardInterrupt
            return original(payload, *args, **kwargs)
        with patch(HTTP, side_effect=reply), patch("cortex.planning.Plan.validate", side_effect=validate):
            first = new_job(self.store, "Compare suppliers", registry_data(), task=task_data(), options={"planning": "always"}, max_model_calls=1)
        with patch(HTTP) as http:
            resumed = resume_job(self.store, first["job_id"])
        http.assert_not_called()
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["budget"]["model_calls_used"], 1)
        self.assertEqual(resumed["budget"]["work_units_used"], 5)

    def test_step_and_provider_transition_states_are_saved(self):
        with patch(HTTP, side_effect=[response("$1"), response("1 unspecified")]):
            result = new_job(self.store, "Draft", registry_data(), plan=plan())
        saved = self.store.read(result["job_id"])
        transitions = self.store.transitions(result["job_id"])
        self.assertEqual(saved["steps"]["first"]["state"], "accepted")
        self.assertEqual(len(saved["steps"]["first"]["history"]), 2)
        states = [row.get("state") for row in transitions if row["event"] == "step_transition"]
        self.assertIn("running", states)
        self.assertIn("drafted", states)
        self.assertIn("accepted", states)
        self.assertEqual(sum(row["event"] == "before_provider_call" for row in transitions), 2)
        self.assertEqual(sum(row["event"] == "after_provider_call" for row in transitions), 2)

    def test_config_input_code_and_counter_changes_rejected(self):
        with patch(HTTP, return_value=response("First")):
            first = self.new(pause_after=1)
        identifier = first["job_id"]
        modified = registry_data()
        modified["models"][1]["priority"] += 1
        with self.assertRaises(JobError):
            resume_job(self.store, identifier, expected_registry=modified)
        with patch("cortex.jobs.code_hashes", return_value={"changed": "value"}), self.assertRaises(JobError):
            resume_job(self.store, identifier)
        saved = self.store.read(identifier)
        saved["budget"]["model_calls"] = 0
        self.store.save(saved, {"event": "test_corrupt_counter"})
        with self.assertRaises(JobError):
            resume_job(self.store, identifier)

    def test_changed_bound_file_and_accepted_output_integrity_rejected(self):
        path = Path(self.folder.name) / "input.txt"
        path.write_text("original")
        import hashlib
        with patch(HTTP, return_value=response("First")):
            first = self.new(pause_after=1, input_files={str(path): hashlib.sha256(path.read_bytes()).hexdigest()})
        path.write_text("changed")
        with self.assertRaises(JobError):
            resume_job(self.store, first["job_id"])
        path.write_text("original")
        saved = self.store.read(first["job_id"])
        saved["steps"]["first"]["event"]["output"]["text"] = "Modified"
        self.store.save(saved, {"event": "test_corrupt_output"})
        with patch(HTTP) as http:
            result = resume_job(self.store, first["job_id"])
        http.assert_not_called()
        self.assertEqual(result["status"], "interrupted")

    def test_known_failed_provider_job_does_not_retry_on_resume(self):
        with patch(HTTP, side_effect=TimeoutError):
            first = new_job(self.store, "Draft", registry_data(), plan=plan())
        with patch(HTTP) as http:
            resumed = resume_job(self.store, first["job_id"], retry_interrupted=True)
        http.assert_not_called()
        self.assertTrue(resumed["terminal_job_reused"])

    def test_os_lock_excludes_second_process_and_releases_after_owner_exit(self):
        with patch(HTTP, return_value=response("First")):
            first = self.new(pause_after=1)
        code = "from cortex.job_store import JobStore,JobError; import sys\ns=JobStore(sys.argv[1])\ntry:\n with s.lock(sys.argv[2]): print('acquired')\nexcept JobError: print('locked')\n"
        with self.store.lock(first["job_id"]):
            other = subprocess.run([sys.executable, "-c", code, str(self.store.path), first["job_id"]], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(other.stdout.strip(), "locked")
        other = subprocess.run([sys.executable, "-c", code, str(self.store.path), first["job_id"]], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(other.stdout.strip(), "acquired")

    def test_credential_material_and_invalid_ids_are_never_stored(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "SECRET-VALUE"}), self.assertRaises(JobError):
            new_job(self.store, "SECRET-VALUE", registry_data(), plan=plan())
        with self.assertRaises(JobError):
            self.store.read("../../escape")

    def test_hard_exit_preserves_inflight_reservation_and_accepted_step(self):
        fixture = Path(self.folder.name) / "fixture.json"
        fixture.write_text(json.dumps({"registry": registry_data(), "plan": plan(independent=True)}))
        code = r'''
import os,sys,json,io
from unittest.mock import patch
from cortex.job_store import JobStore
from cortex.jobs import new_job
s=JobStore(sys.argv[1]); fixture=json.load(open(sys.argv[2])); count=0
def reply(*args,**kwargs):
 global count
 count+=1
 if count==2: os._exit(17)
 return io.BytesIO(json.dumps({'model':'mock-model','done':True,'done_reason':'stop','message':{'content':'First accepted'},'prompt_eval_count':10,'eval_count':5}).encode())
with patch('urllib.request.urlopen',side_effect=reply):
 new_job(s,'Draft two paragraphs',fixture['registry'],plan=fixture['plan'],max_model_calls=3)
'''
        child = subprocess.run([sys.executable, "-c", code, str(self.store.path), str(fixture)], cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 17, child.stderr)
        identifier = self.store.connection.execute("SELECT id FROM jobs").fetchone()[0]
        saved = self.store.read(identifier)
        self.assertEqual(saved["steps"]["first"]["state"], "accepted")
        self.assertEqual(saved["attempts"][-1]["status"], "in_flight")
        self.assertEqual(saved["budget"]["model_calls"], 2)
        with patch(HTTP) as http:
            interrupted = resume_job(self.store, identifier)
        http.assert_not_called()
        self.assertEqual(interrupted["status"], "interrupted")
        with patch(HTTP, return_value=response("Second accepted")) as http:
            resumed = resume_job(self.store, identifier, retry_interrupted=True)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(resumed["status"], "drafted")
        self.assertEqual(resumed["budget"]["model_calls_used"], 3)
        self.assertEqual(resumed["budget"]["work_units_used"], 3)
        self.assertEqual(resumed["step_results"][0]["output"]["text"], "First accepted")

    def test_cli_durable_pause_inspect_resume_and_no_budget_reset_options(self):
        from cortex.product_cli import main as request
        from cortex.job_cli import main as job
        folder = Path(self.folder.name)
        plan_path = folder / "plan.json"
        plan_path.write_text(json.dumps(plan(independent=True)))
        first_stdout = io.StringIO()
        with patch(HTTP, return_value=response("First")), contextlib.redirect_stdout(first_stdout):
            self.assertEqual(request(["Draft two", "--durable", "--db", str(self.store.path), "--plan-file", str(plan_path),
                                      "--pause-after-step", "1", "--max-model-calls", "2"]), 2)
        first = json.loads(first_stdout.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(job(["inspect", first["job_id"], "--db", str(self.store.path)]), 0)
        output = io.StringIO()
        with patch(HTTP, return_value=response("Second")) as http, contextlib.redirect_stdout(output):
            self.assertEqual(job(["resume", first["job_id"], "--db", str(self.store.path)]), 0)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(json.loads(output.getvalue())["budget"]["model_calls_used"], 2)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            job(["resume", first["job_id"], "--db", str(self.store.path), "--max-model-calls", "99"])


if __name__ == "__main__":
    unittest.main()
