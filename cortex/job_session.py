"""Write-ahead reservations and response journaling for opt-in durable execution."""
from dataclasses import asdict
import hashlib
import json

from .policies import Budget
from .providers import ModelResponse, ProviderError


class InterruptedAttempt(RuntimeError):
    pass


class DurableBudget(Budget):
    def __init__(self, session):
        self.session = session
        super().__init__(**session.data["budget"])

    def reserve(self, units=0, model_call=False):
        session = self.session
        key = session.scope
        reused = units and session.reuse_work and key in session.data["work_reservations"]
        if not super().reserve(0 if reused else units, model_call):
            return False
        if units and session.reuse_work:
            session.data["work_reservations"][key] = units
        if model_call:
            session.data["model_usage"].append({"status": "reserved", "role": session.role,
                "provider": None, "model": None, "prompt_tokens": None, "output_tokens": None})
            session.data["attempts"].append({"logical_scope": session.logical_scope, "scope": key,
                "step_id": session.step_id, "status": "in_flight", "response": None,
                "usage_index": self.model_calls - 1, "request_hash": session.request_hash})
        session.sync_budget(self)
        session.save("budget_reserved", scope=key, units=0 if reused else units, model_call=model_call)
        return True

    def replay_provider(self, provider, request, role):
        session = self.session
        fingerprint = hashlib.sha256(json.dumps({"provider": provider, "request": asdict(request), "role": role}, sort_keys=True).encode()).hexdigest()
        session.request_hash = fingerprint
        candidates = [row for row in session.data["attempts"] if row["scope"] == session.scope]
        if not candidates:
            return None
        attempt = candidates[-1]
        if attempt["request_hash"] != fingerprint:
            raise InterruptedAttempt("Stored response request differs; no automatic replay or replacement")
        if attempt["status"] == "response_received" and attempt["response"] is not None:
            session.save("stored_response_reused", scope=session.scope)
            return ModelResponse(**attempt["response"])
        if attempt["status"] == "failed":
            error = session.data["model_usage"][attempt["usage_index"]]["error"]
            raise ProviderError(error["provider"], error["category"], error["status_code"], error["retryable"])
        raise InterruptedAttempt("Reserved provider attempt has unknown outcome")

    def has_stored_response(self):
        return any(row["scope"] == self.session.scope and row["status"] in ("response_received", "failed")
                   for row in self.session.data["attempts"])

    def before_provider_call(self, record, usage):
        index = self.model_calls - 1
        assert usage[-1] is record and usage[index]["status"] == "reserved"
        usage[index] = record
        usage.pop()
        self.session.save("before_provider_call", scope=self.session.scope, usage_index=index)

    def after_provider_call(self, record, response):
        attempt = self.session.data["attempts"][-1]
        attempt["status"] = "response_received" if response is not None else "failed" if "error" in record else "unknown"
        if attempt["status"] == "unknown":
            record.update(status="interrupted_unknown", outcome="unknown")
            if attempt["step_id"] in self.session.data["steps"]:
                self.session.data["steps"][attempt["step_id"]]["state"] = "interrupted"
        attempt["response"] = response.to_dict() if response is not None else None
        self.session.save("after_provider_call", scope=self.session.scope, outcome=attempt["status"])


class JobSession:
    def __init__(self, store, data, retry_interrupted=False, pause_after=None):
        self.store, self.data = store, data
        self.retry_interrupted, self.pause_after = retry_interrupted, pause_after
        self.scope = self.logical_scope = "preparation"
        self.role, self.step_id, self.reuse_work = "unknown", None, False
        self.request_hash = None
        self.finished_this_run = 0
        self.budget = DurableBudget(self)

    def save(self, event, **details):
        self.store.save(self.data, {"event": event, **details})

    def sync_budget(self, budget):
        self.data["budget"] = {key: getattr(budget, key) for key in ("max_units", "max_model_calls", "units", "model_calls")}

    def recover(self):
        for operation in self.data.get('tool_operations',[]):
            if operation['state'] in ('preparing','in_flight'):
                operation['state']='unknown'
                if operation['task_id'] in self.data['steps']:
                    self.data['steps'][operation['task_id']]['state']='interrupted'
        for attempt in self.data["attempts"]:
            if attempt["status"] == "in_flight":
                attempt["status"] = "unknown"
                row = self.data["model_usage"][attempt["usage_index"]]
                row.update(status="interrupted_unknown", outcome="unknown", prompt_tokens=None, output_tokens=None)
                if attempt["step_id"] in self.data["steps"]:
                    self.data["steps"][attempt["step_id"]]["state"] = "interrupted"
        self.data["state"] = "running"
        self.save("execution_acquired", exactly_once=False)

    def set_scope(self, logical_scope, role, step_id=None, reuse_work=True):
        self.logical_scope, self.scope, self.role = logical_scope, logical_scope, role
        self.step_id, self.reuse_work = step_id, reuse_work
        attempts = [row for row in self.data["attempts"] if row["logical_scope"] == logical_scope]
        if attempts:
            last = attempts[-1]
            if last["status"] == "unknown":
                if not self.retry_interrupted:
                    raise InterruptedAttempt("Interrupted attempt outcome is unknown; explicit replacement authorization required")
                if step_id and sum(row["step_id"] == step_id for row in self.data["attempts"]) >= 1 + self.data["draft_policy"]["max_repairs"]:
                    raise InterruptedAttempt("Replacement would exceed the bounded draft-attempt limit")
                self.scope = logical_scope + ":replacement:" + str(len(attempts))
                self.save("unknown_replacement_authorized", scope=self.scope, retained_reserved_call=True)
            else:
                self.scope = last["scope"]

    def prepared(self, classification, plan, trace):
        self.data["classification"] = classification.to_dict() if classification else None
        self.data["plan"] = plan.to_dict() if plan else None
        self.data["trace"] = trace
        if plan:
            for step in plan.to_dict()["steps"]:
                self.data["steps"].setdefault(step["id"], {"state": "pending", "depends_on": step["depends_on"], "history": [], "event": None})
        self.save("preparation_saved")

    def step_saved(self, event, state):
        entry = self.data["steps"][event["task_id"]]
        entry.update(state=state, event=event)
        if state == "accepted":
            entry["accepted_output_sha256"] = hashlib.sha256(json.dumps(event["output"], sort_keys=True).encode()).hexdigest()
        self.save("step_transition", task_id=event["task_id"], state=state)

    def verify_accepted(self, entry):
        value = hashlib.sha256(json.dumps(entry["event"]["output"], sort_keys=True).encode()).hexdigest()
        if value != entry["accepted_output_sha256"] or not entry["event"]["completion_evidence"]["checks_passed"]:
            raise InterruptedAttempt("Stored accepted output failed integrity checks")

    def finish(self, output):
        self.sync_budget(self.budget)
        self.data["result"] = output
        self.data["state"] = "interrupted" if output["status"] == "interrupted" else "accepted" if output["status"] in ("completed", "drafted") else "failed"
        self.save("execution_finished", state=self.data["state"])
