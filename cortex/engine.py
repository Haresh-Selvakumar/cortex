"""Small auditable state machine with policy-independent enforcement."""

from dataclasses import replace
from decimal import Decimal
import json
import time

from .domain import (
    DataError,
    MissingFieldsError,
    ParsingError,
    parse_source,
    calculate,
    verify,
    recommend,
)
from .policies import Budget, LocalPolicy, ModelCallLimit
from .extraction import (
    EXTRACTION_MODES, ExtractionValidationError, ExtractionProviderError,
    verify_extraction,
)


# Illustrative work weights—not measured tokens, dollars or compute.
COSTS = {
    "extract": 1,
    "retry_extract": 1,
    "repair_extract": 2,
    "plan": 3,
    "calculate": 1,
    "repair_calculation": 2,
    "verify": 1,
    "recommend": 1,
    "clarify": 0,
    "stop": 0,
}

ROLES = {
    "extract": "extraction_worker",
    "retry_extract": "extraction_worker",
    "repair_extract": "extraction_worker",
    "plan": "planner",
    "calculate": "calculation_worker",
    "repair_calculation": "calculation_worker",
    "verify": "verifier",
    "recommend": "recommendation_worker",
    "clarify": "governor",
    "stop": "governor",
}

FAULTS = (
    "none",
    "extract_timeout",
    "extract_timeout_persistent",
    "corrupt_extraction",
    "bad_arithmetic",
)

RECOVERY_MODES = ("baseline", "failure-aware")
EXTRACTION_ACTIONS = ("extract", "retry_extract", "repair_extract")


def run(task, policy=None, budget=None, fault="none", recovery_mode="baseline",
        extraction_mode="parser", extraction_worker=None):
    if fault not in FAULTS:
        raise ValueError("Unknown fault")
    if recovery_mode not in RECOVERY_MODES:
        raise ValueError("Unknown recovery mode")
    if extraction_mode not in EXTRACTION_MODES:
        raise ValueError("Unknown extraction mode")
    if extraction_mode != "parser" and extraction_worker is None:
        raise ValueError("Configure an extraction worker for model extraction modes")
    if extraction_worker is not None and extraction_worker.mode != extraction_mode:
        raise ValueError("Extraction mode does not match the worker configuration")

    policy = policy or LocalPolicy()
    budget = budget or Budget()

    quotes = rows = None
    error = None
    verified = False
    attempted = set()
    trace = []
    recovery_action = None
    failure_category = None
    extraction_retries = 0
    timed_out_action = None
    failure_aware = recovery_mode == "failure-aware"
    extraction_report = None

    usage_start = len(policy.usage)
    worker_usage_start = len(extraction_worker.usage) if extraction_worker else 0
    worker_report_start = len(extraction_worker.reports) if extraction_worker else 0
    started = time.perf_counter()

    def finish(status, reason, result=None):
        usage = list(policy.usage)[usage_start:]
        worker_usage = list(extraction_worker.usage)[worker_usage_start:] if extraction_worker else []
        usage.extend(worker_usage)

        known = all(
            record.get("prompt_tokens") is not None
            and record.get("output_tokens") is not None
            for record in usage
        )

        return {
            "version": "0.1.0",
            "policy": policy.name,
            "configuration": {
                "selective": getattr(policy, "selective", False),
                "controller_model": getattr(policy, "model", None),
                "planner_model": getattr(policy, "planner_model", None),
                "prompt_version": getattr(policy, "prompt_version", None),
                "recovery_mode": recovery_mode,
                "extraction_mode": extraction_mode,
                "extractor_model": getattr(extraction_worker, "model", None),
                "extractor_prompt_version": getattr(extraction_worker, "prompt_version", None),
            },
            "fault": fault,
            "status": status,
            "reason": reason,
            "result": result,
            "budget": {
                "work_units_used": budget.units,
                "work_units_limit": budget.max_units,
                "model_calls_used": budget.model_calls,
                "model_calls_limit": budget.max_model_calls,
            },
            "tokens": {
                "prompt": (
                    sum(record["prompt_tokens"] for record in usage)
                    if known else None
                ),
                "output": (
                    sum(record["output_tokens"] for record in usage)
                    if known else None
                ),
                "usage_complete": known,
                "note": (
                    "Provider-reported counts only; "
                    "offline execution has zero model tokens."
                ),
            },
            "model_usage": usage,
            "worker_model_usage": worker_usage,
            "extraction_reports": list(extraction_worker.reports)[worker_report_start:] if extraction_worker else [],
            "trace": trace,
            "latency_ms": round(
                (time.perf_counter() - started) * 1000, 2
            ),
        }

    for step in range(20):
        # Determine legal actions from current progress and failures.
        termination = None
        if recovery_action:
            candidates = [recovery_action, "stop"]

        elif error and failure_aware:
            if failure_category == "transient_execution":
                if extraction_retries < 1:
                    candidates = ["retry_extract", "stop"]
                else:
                    candidates = ["stop"]
                    termination = ("retry_limit", "Extraction timeout persisted after one retry.")
            elif failure_category == "missing_information":
                candidates = ["clarify", "stop"]
            elif failure_category in ("parsing_problem", "source_integrity") and "repair_extract" not in attempted:
                candidates = ["repair_extract", "stop"]
            elif failure_category == "arithmetic_integrity":
                if "repair_calculation" not in attempted:
                    candidates = ["repair_calculation", "stop"]
                else:
                    candidates = ["stop"]
                    termination = ("recovery_limit", "Arithmetic verification failed after bounded recalculation.")
            elif "plan" not in attempted:
                candidates = ["plan", "stop"]
            else:
                candidates = ["stop"]
                termination = ("recovery_limit", "No further supported recovery remains after planning.")

        elif error:
            if error == "missing_information":
                candidates = ["clarify", "stop"]

            elif (
                error == "calculation_mismatch"
                and "repair_calculation" not in attempted
            ):
                candidates = ["repair_calculation", "stop"]

            elif error in ("extraction_failed", "extraction_mismatch"):
                if "repair_extract" not in attempted:
                    candidates = ["repair_extract", "plan", "stop"]
                elif "plan" not in attempted:
                    candidates = ["plan", "clarify", "stop"]
                else:
                    candidates = ["clarify", "stop"]

            else:
                candidates = ["clarify", "stop"]

        elif quotes is None:
            candidates = ["extract", "stop"]

        elif rows is None:
            candidates = ["calculate", "stop"]

        elif not verified:
            candidates = ["verify", "stop"]

        else:
            candidates = ["recommend", "stop"]

        # Remove actions that would exceed the work budget.
        candidates = [
            action
            for action in candidates
            if budget.units + COSTS[action] <= budget.max_units
        ]

        error_detail = (
            next(
                (
                    item["detail"]
                    for item in reversed(trace)
                    if item.get("status") == "failed"
                    and item.get("detail")
                ),
                None,
            )
            if error else None
        )

        state = {
            "step": step,
            "error": error,
            "error_detail": error_detail,
            "verified": verified,
            "quotes_count": len(quotes or []),
            "rows_count": len(rows or []),
            "remaining_units": budget.max_units - budget.units,
            "remaining_model_calls": budget.max_model_calls - budget.model_calls,
            "attempted_actions": sorted(attempted),
            "admissible_actions": candidates,
            "action_costs": {
                action: COSTS[action] for action in candidates
            },
        }
        # Preserve selective-v1 model input for the baseline comparison.
        if failure_aware:
            state.update(
                failure_category=failure_category,
                extraction_retries=extraction_retries,
                extraction_retry_limit=1,
                timed_out_action=timed_out_action,
                recovery_mode=recovery_mode,
            )

        if candidates == ["stop"]:
            if failure_aware:
                trace.append({
                    "step": step, "action": "stop", "role": "governor",
                    "decision_source": "rule", "failure_category": failure_category,
                    "decision_failure_category": failure_category,
                    "work_units": 0, "status": "ok",
                    "reason": termination[1] if termination else "Insufficient work budget for the required action.",
                })
            if termination:
                return finish(*termination)
            return finish(
                "budget_exhausted",
                "Insufficient work budget for the next required action.",
            )

        calls_before = budget.model_calls
        try:
            progress_actions = [action for action in candidates if action != "stop"]
            if failure_aware and len(progress_actions) == 1 and progress_actions[0] != "plan":
                action = progress_actions[0]
                reason = {
                    "retry_extract": "Transient extraction timeout: retry the failed operation once.",
                    "repair_extract": "Use the supported flexible parser without inventing source values.",
                    "repair_calculation": "Recalculate once after an arithmetic verification failure.",
                    "clarify": "Required information remains missing after alias handling or planner review.",
                }.get(action, "Rule selected the sole admissible non-stop action.")
            elif hasattr(policy, "model"):
                action, reason = policy.choose(state, candidates, budget=budget)
            else:
                action, reason = policy.choose(state, candidates)
        except ModelCallLimit as exc:
            if failure_aware:
                trace.append({
                    "step": step, "action": "stop", "role": "governor",
                    "decision_source": "rule", "failure_category": failure_category,
                    "decision_failure_category": failure_category,
                    "work_units": 0, "status": "blocked", "reason": str(exc),
                })
            return finish("budget_exhausted", str(exc))
        except Exception as exc:
            trace.append({
                "step": step,
                "action": "controller_failure",
                "status": "failed",
                "error_type": type(exc).__name__,
                "decision_source": "model" if budget.model_calls > calls_before else "rule",
                "decision_failure_category": failure_category,
                "failure_category": "controller_failure",
            })
            return finish(
                "controller_failed",
                "Controller unavailable or invalid; no silent fallback.",
            )

        if action not in candidates:
            if failure_aware:
                trace.append({
                    "step": step, "action": "controller_failure", "status": "failed",
                    "decision_source": "model" if budget.model_calls > calls_before else "rule",
                    "decision_failure_category": failure_category,
                    "failure_category": "controller_failure",
                })
            return finish(
                "controller_failed",
                "Controller selected an inadmissible action.",
            )

        budget.reserve(COSTS[action])

        event = {
            "step": step,
            "action": action,
            "role": ROLES[action],
            "reason": reason,
            "work_units": COSTS[action],
            "status": "ok",
            "decision_source": (
                "model" if budget.model_calls > calls_before else "rule"
            ),
            "decision_failure_category": failure_category,
            "failure_category": failure_category,
        }
        trace.append(event)

        recovery_action = None
        attempted.add(action)

        if action == "stop":
            return finish(
                "stopped",
                "Controller stopped before verified completion.",
            )

        if action == "clarify":
            return finish(
                "needs_clarification",
                "Supply complete, valid quote fields; unknown data is not inferred.",
            )

        worker_calls_before = budget.model_calls
        worker_reports_before = len(extraction_worker.reports) if extraction_worker else 0
        try:
            if action in EXTRACTION_ACTIONS:
                if action == "retry_extract":
                    extraction_retries += 1
                    event["extraction_retry_number"] = extraction_retries
                    event["retry_of"] = timed_out_action
                if (action == "extract" and fault == "extract_timeout") or fault == "extract_timeout_persistent":
                    raise TimeoutError("Injected extraction timeout")

                robust = action == "repair_extract" or (action == "retry_extract" and timed_out_action == "repair_extract")
                worker_calls_before = budget.model_calls
                if extraction_worker is None:
                    quotes = parse_source(task.source, robust=robust)
                else:
                    quotes, extraction_report = extraction_worker.extract(task.source, budget, robust=robust)
                    event.update(
                        worker_id=extraction_report["worker_id"],
                        worker_model=extraction_report["model"],
                        worker_prompt_version=extraction_report["prompt_version"],
                        worker_execution_source="model" if budget.model_calls > worker_calls_before else "rule",
                        extraction_provenance=extraction_report.get("provenance"),
                        verification_outcome=extraction_report.get("verification"),
                    )

                if action == "extract" and fault == "corrupt_extraction":
                    quotes[0] = replace(
                        quotes[0],
                        shipping=quotes[0].shipping + Decimal("500"),
                    )

                rows = None
                verified = False
                error = None
                failure_category = None

            elif action == "plan":
                options = [
                    option for option in ("repair_extract", "clarify")
                    if budget.units + COSTS[option] <= budget.max_units
                    and (not failure_aware or option != "repair_extract" or option not in attempted)
                ]

                if hasattr(policy, "plan"):
                    # Planner options and budget must describe its own decision.
                    planner_state = dict(
                        state,
                        source=task.source,
                        admissible_actions=options,
                        action_costs={
                            option: COSTS[option] for option in options
                        },
                        remaining_units=budget.max_units - budget.units,
                        remaining_model_calls=budget.max_model_calls - budget.model_calls,
                        attempted_actions=sorted(attempted),
                    )

                    planner_calls_before = budget.model_calls
                    try:
                        recovery_action, explanation = policy.plan(
                            planner_state, options, budget=budget
                        )
                        if recovery_action not in options:
                            raise ValueError("Planner selected an inadmissible recovery")
                    except ModelCallLimit as exc:
                        event.update(status="blocked", error="model_call_cap")
                        event["planner_decision_source"] = "rule"
                        return finish("budget_exhausted", str(exc))
                    except Exception as exc:
                        event.update(status="failed", error_type=type(exc).__name__)
                        event["failure_category"] = "controller_failure"
                        event["planner_decision_source"] = (
                            "model" if budget.model_calls > planner_calls_before else "rule"
                        )
                        return finish(
                            "controller_failed",
                            "Planner unavailable or invalid; no silent fallback.",
                        )

                    event["planner_reason"] = explanation
                    event["planner_decision_source"] = (
                        "model" if budget.model_calls > planner_calls_before else "rule"
                    )

                else:
                    if failure_aware and "repair_extract" not in options:
                        recovery_action = "clarify"
                    else:
                        try:
                            parse_source(task.source, robust=True)
                            recovery_action = "repair_extract"
                        except DataError:
                            recovery_action = "clarify"

                    event["planner_reason"] = (
                        "Local feasibility probe, not LLM reasoning."
                    )
                    event["planner_decision_source"] = "rule"

                event["planner_action"] = recovery_action

            elif action in ("calculate", "repair_calculation"):
                rows = calculate(task, quotes)

                if action == "calculate" and fault == "bad_arithmetic":
                    rows[0]["total"] = str(
                        Decimal(rows[0]["total"]) + 1
                    )

                verified = False
                error = None
                failure_category = None

            elif action == "verify":
                if extraction_report and extraction_report["model"] is not None:
                    if not verify_extraction(task.source, extraction_report, quotes):
                        error = "extraction_mismatch"
                    else:
                        reference = parse_source(json.dumps(extraction_report["quotes"]), robust=True)
                        error = verify(task, quotes, rows, reference_quotes=reference)
                    event["verification_outcome"] = {
                        "status": "passed" if error is None else "failed",
                        "arithmetic_and_feasibility": error is None,
                        "source_interpretation_proven": False,
                        "limitations": "Schema, bounded support and excerpts are checked; semantic/source authenticity is not proved.",
                    }
                else:
                    error = verify(task, quotes, rows)
                    if extraction_report:
                        event["verification_outcome"] = {
                            "status": "passed" if error is None else "failed",
                            "structured_source_match": error != "extraction_mismatch",
                            "arithmetic_and_feasibility": error is None,
                        }
                verified = error is None

                if error:
                    event["status"] = "failed"
                    event["error"] = error
                    failure_category = {
                        "calculation_mismatch": "arithmetic_integrity",
                        "extraction_mismatch": "source_integrity",
                    }.get(error, "unclassified")
                    event["failure_category"] = failure_category

            elif action == "recommend":
                result = recommend(rows)
                return finish(
                    result["status"],
                    result["reason"],
                    result,
                )

        except ModelCallLimit as exc:
            event.update(status="blocked", error="model_call_cap")
            if extraction_worker and extraction_worker.reports:
                report = extraction_worker.reports[-1]
                event.update(worker_id=report["worker_id"], worker_model=report["model"],
                             worker_execution_source="rule", verification_outcome=report.get("verification"))
            return finish("budget_exhausted", str(exc))

        except ExtractionProviderError as exc:
            event.update(status="failed", failure_category="provider_failure",
                         worker_id=exc.report["worker_id"], worker_model=exc.report["model"],
                         worker_execution_source="model" if budget.model_calls > worker_calls_before else "rule",
                         verification_outcome=exc.report.get("verification"))
            return finish("worker_failed", "Extraction provider failed; no retry or silent fallback.")

        except ExtractionValidationError as exc:
            event.update(status="failed", failure_category=exc.kind,
                         worker_id=exc.report["worker_id"], worker_model=exc.report["model"],
                         worker_execution_source="model" if budget.model_calls > worker_calls_before else "rule",
                         extraction_provenance=exc.report.get("provenance"),
                         verification_outcome=exc.report.get("verification"))
            trace.append({"step": step, "action": "clarify", "role": "governor",
                          "reason": "Extraction has missing, conflicting or unsupported values; do not guess.",
                          "work_units": 0, "status": "ok", "decision_source": "rule",
                          "decision_failure_category": exc.kind, "failure_category": exc.kind})
            return finish("needs_clarification", "Clarify extraction values/evidence before calculation.")

        except MissingFieldsError as exc:
            error = (
                "missing_information"
                if exc.aliases_checked
                else "extraction_failed"
            )
            failure_category = "missing_information" if exc.aliases_checked else "parsing_problem"

            event.update(
                status="failed",
                error=error,
                detail=str(exc)[:200],
                missing_fields=list(exc.fields),
                aliases_checked=exc.aliases_checked,
                failure_category=failure_category,
            )

        except TimeoutError as exc:
            error = "extraction_failed" if action in EXTRACTION_ACTIONS else "worker_failed"
            failure_category = "transient_execution" if action in EXTRACTION_ACTIONS else "unclassified"
            if action in EXTRACTION_ACTIONS and action != "retry_extract":
                timed_out_action = action
            event.update(status="failed", error=error, detail=str(exc)[:200],
                         error_type=type(exc).__name__, failure_category=failure_category)

        except (
            DataError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            error = (
                "extraction_failed"
                if action in EXTRACTION_ACTIONS
                else "worker_failed"
            )
            failure_category = "parsing_problem" if isinstance(exc, ParsingError) else "unclassified"

            event.update(
                status="failed",
                error=error,
                detail=str(exc)[:200],
                error_type=type(exc).__name__,
                failure_category=failure_category,
            )

        except Exception as exc:
            failure_category = "unclassified"
            event.update(
                status="failed",
                error_type=type(exc).__name__,
                failure_category=failure_category,
            )
            if failure_aware:
                error = "extraction_failed" if action in EXTRACTION_ACTIONS else "worker_failed"
                event["error"] = error
                continue
            return finish(
                "worker_failed",
                "Unexpected worker failure; execution stopped.",
            )

        finally:
            if action in EXTRACTION_ACTIONS and extraction_worker and len(extraction_worker.reports) > worker_reports_before:
                report = extraction_worker.reports[-1]
                event.update(
                    worker_id=report["worker_id"], worker_model=report["model"],
                    worker_prompt_version=report["prompt_version"],
                    worker_execution_source="model" if budget.model_calls > worker_calls_before else "rule",
                    extraction_provenance=report.get("provenance"),
                    verification_outcome=report.get("verification"),
                )

    return finish(
        "step_limit",
        "Maximum transition count reached.",
    )
