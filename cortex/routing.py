"""Capability eligibility first; configured priority is a transparent tie-break."""

from dataclasses import dataclass, asdict
from .extraction import source_kind

TEXT_WORKER_CAPABILITIES = {"analysis", "text_generation", "code_generation", "document_draft"}


@dataclass(frozen=True)
class Worker:
    id: str
    kind: str
    model_id: str | None
    capabilities: tuple
    task_types: tuple
    minimum_units: int
    model_calls_required: int
    priority: int

    def to_dict(self):
        return asdict(self)


def route(classification, task, runtime, budget, trace, extraction_mode="parser", override=None, supplier_context=False):
    workers = []
    supplier = "supplier_analysis" in classification.required_capabilities or supplier_context
    structured = task is not None and source_kind(task.source) != "plain_text"
    if supplier and task is not None:
        # Admission requires the first action, not a guessed full recovery cost.
        # The unchanged engine enforces every later action/verification budget.
        if extraction_mode == "parser" or extraction_mode == "parser-first" and structured:
            workers.append(Worker("supplier-parser", "supplier_parser", None, ("analysis", "supplier_analysis"),
                                  ("analysis",), 1, 0, -1))
        for spec in runtime.registry.models.values():
            if spec.structured_output and "quote_extraction" in spec.capabilities:
                workers.append(Worker("supplier-model:" + spec.id, "supplier_model", spec.id,
                                      ("analysis", "supplier_analysis"), spec.task_types, 1, 1, spec.priority))
    elif not supplier:
        for spec in runtime.registry.models.values():
            effective = tuple(sorted(set(spec.capabilities).intersection(TEXT_WORKER_CAPABILITIES)))
            workers.append(Worker("text-model:" + spec.id, "text_draft", spec.id, effective,
                                  spec.task_types, 1, 1, spec.priority))
    eligibility, eligible = [], []
    for worker in workers:
        spec = runtime.registry.models.get(worker.model_id)
        reasons = []
        if classification.task_type not in worker.task_types:
            reasons.append("task_type_not_supported")
        missing = sorted(set(classification.required_capabilities) - set(worker.capabilities))
        if missing:
            reasons.append("missing_capabilities:" + ",".join(missing))
        availability = runtime.available(spec) if spec else {"available": True, "reason": "deterministic_local_tool", "connectivity_verified": True}
        if not availability["available"]:
            reasons.append("worker_unavailable")
        if budget.max_units - budget.units < worker.minimum_units:
            reasons.append("insufficient_work_budget")
        if budget.max_model_calls - budget.model_calls < worker.model_calls_required:
            reasons.append("insufficient_model_call_budget")
        row = {**worker.to_dict(), "eligible": not reasons, "rejections": reasons,
               "availability": availability, "missing_capabilities": missing,
               "model": spec.to_dict() if spec else None}
        eligibility.append(row)
        if not reasons:
            eligible.append(worker)
    if override:
        eligible = [worker for worker in eligible if worker.model_id == override]
        reason = "Explicit registered model override; all eligibility checks still apply."
    else:
        # Never use a model for structured extraction in parser-only mode.
        if supplier and extraction_mode == "parser":
            eligible = [worker for worker in eligible if worker.kind == "supplier_parser"]
        preferred = runtime.registry.roles.get("worker")
        if preferred and any(worker.model_id == preferred for worker in eligible):
            eligible = [worker for worker in eligible if worker.model_id == preferred]
        reason = "Eligible worker with lowest configured priority; deterministic supplier parser takes precedence by default."
    chosen = min(eligible, key=lambda worker: (worker.priority, worker.id)) if eligible else None
    trace.append({"stage": "routing", "decision_source": "rule", "override": override,
                  "context_rule": "bounded_supplier_intent_with_attachment" if supplier_context else None,
                  "required_capabilities": list(classification.required_capabilities),
                  "remaining_units": budget.max_units - budget.units,
                  "remaining_model_calls": budget.max_model_calls - budget.model_calls,
                  "eligibility": eligibility, "selected": chosen.to_dict() if chosen else None,
                  "reason": reason if chosen else "No eligible worker; no silent override or capability fallback.",
                  "trained_selector": False, "cost_optimality_established": False})
    return chosen
