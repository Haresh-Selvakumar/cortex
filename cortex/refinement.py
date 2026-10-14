"""Bounded generation/check/repair, with rejected outputs kept out of dependencies."""
import hashlib
from .draft_checks import check_draft, repair_prompt
from .job_session import InterruptedAttempt


def checked_draft(worker, step, prompt, executor, policy, constraints, event, budget, session=None):
    history = session.data["steps"][step["id"]]["history"] if session else []
    event["refinement_history"] = history
    previous = None
    current_prompt = prompt
    for number in range(1 + policy.max_repairs):
        if session:
            session.set_scope("step:" + step["id"] + ":draft:" + str(number),
                              "worker" if number == 0 else "draft_repair", step["id"], True)
        if number < len(history):
            execution = history[number]["execution"]
        else:
            if session and not budget.has_stored_response() and sum(row["step_id"] == step["id"] for row in session.data["attempts"]) >= 1 + policy.max_repairs:
                event["refinement_stop"] = "repair_limit_including_unknown_attempts"
                return ({"status": "refinement_failed", "reason": "Draft attempt limit reached, including unknown attempts.", "result": None, "completion_evidence": {}}, False, "refinement_failed")
            if not getattr(budget, "has_stored_response", lambda: False)() and (budget.model_calls >= budget.max_model_calls or budget.units >= budget.max_units):
                event["refinement_stop"] = "budget_exhausted"
                return ({"status": "budget_exhausted", "reason": "No budget for generation/refinement.", "result": None, "completion_evidence": {}}, False, "budget_exhausted")
            if session:
                session.step_saved(event, "running")
            execution = executor(worker, step, current_prompt)
            history.append({"attempt": number, "purpose": "generation" if number == 0 else "repair",
                            "prompt": current_prompt, "execution": execution, "check_report": None})
            if session:
                session.step_saved(event, "drafted" if execution["status"] == "drafted" else "failed")
        if execution["status"] != "drafted":
            return execution, False, execution["status"]
        text = execution["result"]["text"]
        report = check_draft(text, policy, constraints)
        history[number]["check_report"] = report
        history[number]["draft_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        event["draft_check_report"] = report
        if session:
            session.step_saved(event, "drafted")
        if report["passed"]:
            return execution, True, "accepted"
        if previous is not None and text.strip() == previous.strip():
            event["refinement_stop"] = "repeated_without_progress"
            return execution, False, "refinement_failed"
        if number >= policy.max_repairs:
            event["refinement_stop"] = "repair_limit"
            return execution, False, "refinement_failed"
        previous = text
        try:
            current_prompt = repair_prompt(prompt, text, report, policy)
        except ValueError:
            event["refinement_stop"] = "repair_context_limit"
            return execution, False, "context_limit"
    raise AssertionError("Bounded repair loop escaped")
