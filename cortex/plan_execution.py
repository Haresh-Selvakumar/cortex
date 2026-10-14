"""Sequential topological execution; saved traces are not resumable state."""

from .classifier import Classification
from .prompts import build_worker_prompt
from .routing import route, Worker
from .refinement import checked_draft
from .job_session import InterruptedAttempt

SUCCESS = {"completed", "no_feasible_supplier", "drafted"}


def execute_plan(plan, request, task, constraints, source, runtime, budget, trace, executor,
                 extraction_mode="parser", worker_model=None, draft_policy=None, session=None, tools=None):
    by_id = {step["id"]: step for step in plan.to_dict()["steps"]}
    results = {}
    for identifier in plan.order:
        step = by_id[identifier]
        saved = session.data["steps"].get(identifier) if session else None
        previous_event = saved.get("event") if saved else None
        recheck_dependency = saved and saved["state"] == "blocked" and previous_event["status"] == "blocked_dependency" and all(results[dep]["completion_evidence"]["checks_passed"] for dep in step["depends_on"])
        if saved and saved["state"] in ("accepted", "failed", "blocked") and not recheck_dependency:
            if saved["state"] == "accepted":
                session.verify_accepted(saved)
            results[identifier] = saved["event"]
            trace.append({"stage": "durable_step_reused", "task_id": identifier, "state": saved["state"], "model_call": False})
            continue
        if session and session.pause_after is not None and session.finished_this_run >= session.pause_after:
            break
        event = {"stage": "plan_step", "task_id": identifier, "task_type": step["task_type"],
                 "depends_on": step["depends_on"], "decision_source": "rule",
                 "status": "pending", "worker_prompt": None, "routing": [], "execution": None,
                 "output": None, "completion_evidence": {"checks_passed": False, "external_actions_performed": False}}
        trace.append(event)
        results[identifier] = event
        if session:
            session.step_saved(event, "pending")
        failed = [dep for dep in step["depends_on"] if not results[dep]["completion_evidence"]["checks_passed"]]
        if failed:
            event.update(status="blocked_dependency", reason="Prerequisite did not pass its declared completion checks.", failed_dependencies=failed)
            if session:
                session.step_saved(event, "blocked")
            continue
        if step["execution_kind"] == "unsupported":
            event.update(status="unsupported", reason="No actual tool implements the requested capability.",
                         unsupported_capabilities=step["required_capabilities"])
            event["routing"].append({"stage": "routing", "decision_source": "rule", "selected": None,
                                     "reason": "Actual executable capability absent; model tool declarations do not grant tools."})
            if session:
                session.step_saved(event, "blocked")
            continue
        missing = list(step["missing_essential_information"])
        inputs = set(step["required_inputs"])
        if "supplier_task" in inputs and task is None:
            missing.append("supplier_task")
        if "source_material" in inputs and not (source or task and task.source):
            missing.append("source_material")
        if "constraints" in inputs and not constraints:
            missing.append("constraints")
        if missing:
            event.update(status="needs_clarification", reason="Step is missing essential inputs.", missing_essential_information=missing)
            if session:
                session.step_saved(event, "blocked")
            continue
        predecessors = [{"task_id": dep, "status": results[dep]["status"], "output": results[dep]["output"],
                         "completion_evidence": results[dep]["completion_evidence"]} for dep in step["depends_on"]]
        if step['execution_kind']=='tool':
            from .tool_execution import execute_tool
            from .tools import ToolError
            try:
                output = execute_tool(tools,step,predecessors,session,event)
                event.update(status='completed',output=output,reason='Registered tool completion check passed.')
                event['completion_evidence']={'checks_passed':True,'checks':['tool_verified'],
                    'external_actions_performed':True,'validation_scope':tools.catalog()[step['tool']['name']]['completion']}
                session.step_saved(event,'accepted')
            except ToolError as exc:
                event.update(status='interrupted' if exc.category=='tool_outcome_unknown' else 'needs_clarification' if exc.missing else 'budget_exhausted' if 'budget_exhausted' in exc.category else 'tool_failed',
                             reason=exc.category,missing_essential_information=exc.missing)
                session.step_saved(event,'interrupted' if event['status']=='interrupted' else 'blocked' if exc.missing else 'failed')
            session.finished_this_run+=1
            continue
        try:
            prompt = build_worker_prompt(request, step, task, constraints, source, predecessors)
        except ValueError:
            event.update(status="context_limit", reason="Required context exceeds the prompt bound; no truncation or invocation.")
            if session:
                session.step_saved(event, "failed")
            continue
        event["worker_prompt"] = prompt
        if tools is not None and tools.config['event_requirements']:
            import json
            data = json.loads(prompt['data'])
            data['AUTHORITATIVE_EVENT_REQUIREMENTS'] = tools.config['event_requirements']
            prompt = {**prompt,'data':json.dumps(data,ensure_ascii=False)}
            event['worker_prompt']=prompt
        if step["execution_kind"] == "text_draft" and draft_policy is not None:
            import json
            data = json.loads(prompt["data"])
            data["DECLARED_DRAFT_CHECKS"] = draft_policy.to_dict()
            prompt = {**prompt, "data": json.dumps(data, ensure_ascii=False), "prompt_version": "checked-draft-v1",
                      "system": prompt["system"] + " Satisfy the declared code checks. Section labels must be standalone headings. Unknown currency permits no denomination. Word count uses whitespace-separated tokens."}
            event["worker_prompt"] = prompt
        classification = Classification(step["task_type"], tuple(step["required_capabilities"]), (), False, False)
        replayable = session and (saved["history"] or any(row["step_id"] == identifier and row["status"] in ("response_received", "failed", "unknown") for row in session.data["attempts"]))
        selected = previous_event["routing"][-1].get("selected") if previous_event and previous_event["routing"] else None
        if replayable and selected is not None:
            worker = Worker(**selected)
            event["routing"] = previous_event["routing"]
        else:
            worker = route(classification, task, runtime, budget, event["routing"], extraction_mode, worker_model)
        if worker is None:
            considered = [row for row in event["routing"][-1]["eligibility"] if not worker_model or row["model_id"] == worker_model]
            budget_blocked = any(row["rejections"] and all(reason.startswith("insufficient_") for reason in row["rejections"]) for row in considered)
            event.update(status="budget_exhausted" if budget_blocked else "no_eligible_worker",
                         reason="No eligible worker; capabilities, availability, override and remaining budget are enforced.")
            if session:
                session.step_saved(event, "failed")
            continue
        refined_valid = None
        try:
            if step["execution_kind"] == "text_draft" and draft_policy is not None:
                execution, refined_valid, checked_status = checked_draft(worker, step, prompt, executor, draft_policy, constraints, event, budget, session)
            else:
                if session:
                    session.set_scope("step:" + identifier + ":supplier", "extraction_worker", identifier, False)
                    session.step_saved(event, "running")
                execution = executor(worker, step, prompt)
        except InterruptedAttempt as exc:
            event.update(status="interrupted", reason=str(exc))
            if session:
                session.step_saved(event, "interrupted")
            continue
        event["execution"] = execution
        event.update(status=execution["status"], reason=execution["reason"])
        output = execution["result"]
        evidence = execution["completion_evidence"]
        if step["execution_kind"] == "text_draft":
            valid = (execution["status"] == "drafted" and type(output) is dict
                     and type(output.get("text")) is str and bool(output["text"].strip()) and len(output["text"].encode()) <= 16_000)
            scope = "text_shape_only_not_semantic_correctness"
            if refined_valid is not None:
                valid = refined_valid
                event["status"] = "accepted" if valid else checked_status
                scope = "declared_code_checks_only_not_general_factual_verification"
        else:
            valid = (execution["status"] in ("completed", "no_feasible_supplier")
                     and evidence.get("supplier_verification_passed") is True
                     and (execution["status"] == "no_feasible_supplier" or type(output) is dict and output.get("supplier") is not None and output.get("total") is not None))
            scope = "existing_supplier_source_arithmetic_and_feasibility_gate"
            if valid and output is None:
                output = {"outcome": "no_feasible_supplier"}
        event["completion_evidence"] = {**evidence, "checks_passed": bool(valid),
                                         "checks": step["completion_checks"], "validation_scope": scope,
                                         "external_actions_performed": False}
        if valid:
            event["output"] = output
        elif execution["status"] in SUCCESS:
            event.update(status="verification_failed", reason="Output failed the declared completion gate.")
        if refined_valid is not None and not valid:
            event["status"] = checked_status
        if session:
            session.step_saved(event, "accepted" if valid else "failed")
            session.finished_this_run += 1
    steps = [results[identifier] for identifier in plan.order if identifier in results]
    statuses = {event["status"] for event in steps}
    if len(steps) < len(plan.order) or "interrupted" in statuses:
        status, reason = "interrupted", "Execution paused or an in-flight attempt has unknown outcome; accepted work is retained."
    elif all(event["completion_evidence"]["checks_passed"] for event in steps):
        status = "drafted" if "drafted" in statuses or any(by_id[event["task_id"]]["execution_kind"] == "text_draft" for event in steps) else "completed"
        reason = "All declared completion checks passed; drafts remain semantically unverified."
    else:
        status = next((value for value in ("budget_exhausted", "unsupported", "needs_clarification") if value in statuses), "partial" if any(event["completion_evidence"]["checks_passed"] for event in steps) else "plan_failed")
        reason = "Some plan steps did not pass completion checks; dependent steps were blocked."
    return {"status": status, "reason": reason, "steps": steps,
            "result": {"steps": [{"task_id": event["task_id"], "status": event["status"], "output": event["output"]} for event in steps]}}
