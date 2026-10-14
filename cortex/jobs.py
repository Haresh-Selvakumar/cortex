"""Create, inspect and resume opt-in jobs with immutable execution bindings."""
import hashlib
import json
import os
from pathlib import Path

from .domain import Task
from .draft_checks import DraftPolicy
from .job_session import JobSession, InterruptedAttempt
from .job_store import JobError
from .policies import Budget
from .product import execute_request
from .registry import Registry


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def code_hashes():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path(__file__).parent.glob("*.py"))}


def resolved_registry(registry):
    return {"providers": registry.providers, "roles": registry.roles,
            "models": {key: value.to_dict() for key, value in registry.models.items()}}


def immutable(data):
    return {key: data[key] for key in ("request", "inputs", "options", "registry_payload", "draft_policy", "limits")}


def new_job(store, request, registry_payload, task=None, plan=None, constraints=(), source="",
            policy=None, options=None, max_units=20, max_model_calls=12, input_files=None,
            pause_after=None, prepare_only=False, on_created=None):
    registry = Registry(registry_payload)
    policy = policy or DraftPolicy.validate(currency=(task or {}).get("currency"))
    policy = DraftPolicy.validate({"max_words": policy.max_words, "required_sections": list(policy.required_sections)}, policy.currency, policy.max_repairs)
    if task is not None:
        Task.from_dict(task)
        if task.get("currency") is not None and task["currency"] != policy.currency:
            raise JobError("Draft currency differs from authoritative task currency")
    options = dict(options or {})
    allowed = {"classifier_mode", "classifier_model", "worker_model", "planner_model", "extraction_mode", "recovery_mode", "fault", "offline", "probe", "planning", "request_contract", "tool_config"}
    if set(options) - allowed:
        raise JobError("Unsupported durable execution option")
    if options.get('tool_config') is not None:
        from .tools import ToolRegistry
        options['tool_config']=ToolRegistry(options['tool_config']).config
    if options.get("planning", "off") == "off":
        options["planning"] = "auto"
    Budget(max_units, max_model_calls)
    from .prompts import validate_context
    validate_context(request, constraints, source)
    data = {"request": request, "registry_payload": registry_payload,
        "inputs": {"task": task, "plan": plan, "constraints": list(constraints), "source": source},
        "options": options, "draft_policy": policy.to_dict(), "limits": {"max_units": max_units, "max_model_calls": max_model_calls},
        "budget": {"max_units": max_units, "max_model_calls": max_model_calls, "units": 0, "model_calls": 0},
        "steps": {}, "trace": [], "attempts": [], "model_usage": [], "work_reservations": {},
        "classification": None, "plan": None, "result": None}
    rendered = json.dumps(data)
    credential_names = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"} | {settings.get("api_key_env") for settings in registry.providers.values() if settings.get("api_key_env")}
    if any(os.environ.get(name) and (os.environ[name] in rendered or json.dumps(os.environ[name])[1:-1] in rendered) for name in credential_names):
        raise JobError("Credential material cannot be stored in job inputs")
    data["bindings"] = {"immutable_sha256": digest(immutable(data)), "resolved_registry_sha256": digest(resolved_registry(registry)),
                        "code_sha256": code_hashes(), "input_files": input_files or {}}
    identifier = store.create(data)
    if on_created is not None:
        on_created(identifier)
    return resume_job(store, identifier, pause_after=pause_after, prepare_only=prepare_only)


def resume_job(store, identifier, retry_interrupted=False, pause_after=None, expected_registry=None, prepare_only=False):
    with store.lock(identifier):
        data = store.read(identifier)
        registry = Registry(data["registry_payload"] if expected_registry is None else expected_registry)
        if (digest(immutable(data)) != data["bindings"]["immutable_sha256"]
                or digest(resolved_registry(registry)) != data["bindings"]["resolved_registry_sha256"]
                or code_hashes() != data["bindings"]["code_sha256"]):
            raise JobError("Incompatible job inputs, code or resolved model configuration")
        transitions=store.transitions(identifier)
        reservations = [row for row in transitions if row["event"] == "budget_reserved"]
        if data['options'].get('tool_config') is not None:
            intents=[row['operation_id'] for row in transitions if row['event']=='tool_intent_reserved']
            operations=[row['operation_id'] for row in data.get('tool_operations',[])]
            if intents!=operations or len(operations)>data['options']['tool_config']['max_operations']:
                raise JobError('Tool operation counters differ from the reservation journal')
        if (data["budget"]["max_units"] != data["limits"]["max_units"]
                or data["budget"]["max_model_calls"] != data["limits"]["max_model_calls"]
                or data["budget"]["units"] != sum(row["units"] for row in reservations)
                or data["budget"]["model_calls"] != sum(int(row["model_call"]) for row in reservations)
                or data["budget"]["model_calls"] != len(data["model_usage"])):
            raise JobError("Stored budget counters differ from the cumulative reservation journal")
        for path, expected in data["bindings"]["input_files"].items():
            try:
                actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                raise JobError("A bound input/configuration file is unavailable") from None
            if actual != expected:
                raise JobError("A bound input/configuration file changed")
        if data["state"] in ("accepted", "failed") and data["result"] is not None:
            result = dict(data["result"])
            result["terminal_job_reused"] = True
            return result
        if pause_after is not None and (type(pause_after) is not int or pause_after < 1):
            raise JobError("Pause point must be a positive completed-step count")
        policy_data = data["draft_policy"]
        policy = DraftPolicy.validate({"max_words": policy_data["max_words"], "required_sections": policy_data["required_sections"]},
                                      policy_data["currency"], policy_data["max_repairs"])
        session = JobSession(store, data, retry_interrupted, pause_after)
        session.prepare_only = prepare_only
        session.recover()
        inputs = data["inputs"]
        try:
            return execute_request(data["request"], registry, Task.from_dict(inputs["task"]) if inputs["task"] else None,
                                   session.budget, **data["options"], plan_payload=inputs["plan"],
                                   constraints=inputs["constraints"], source_material=inputs["source"],
                                   draft_policy=policy, session=session)
        except (InterruptedAttempt, KeyboardInterrupt) as exc:
            output = {"product_version": "durable-refinement-v1", "job_id": identifier, "status": "interrupted",
                      "reason": str(exc) if isinstance(exc, InterruptedAttempt) else "Execution interrupted by user.",
                      "budget": {"work_units_used": session.budget.units, "model_calls_used": session.budget.model_calls,
                                 "work_units_limit": session.budget.max_units, "model_calls_limit": session.budget.max_model_calls},
                      "model_usage": data["model_usage"], "plan": data["plan"], "trace": data["trace"],
                      "result": None, "resumable": True, "exactly_once": False}
            if data['options'].get('tool_config') is not None:
                output.update(tool_operations=data.get('tool_operations',[]),
                    tool_accounting={'operations_used':len(data.get('tool_operations',[])),
                        'operations_limit':data['options']['tool_config']['max_operations'],'model_calls_for_tools':0})
            session.finish(output)
            return output
