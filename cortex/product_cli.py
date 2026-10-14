"""New request entry point; the original supplier CLI remains available."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform

from .domain import Task
from .engine import FAULTS
from .policies import Budget
from .product import execute_request
from .registry import Registry


def main(argv=None):
    parser = argparse.ArgumentParser(description="Cortex request -> classify -> route -> worker -> evidence")
    parser.add_argument("request")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config" / "cortex.example.json")
    parser.add_argument("--task", type=Path, help="Existing supplier task JSON attachment")
    parser.add_argument("--classifier", choices=("auto", "ai", "rules"), default="auto")
    parser.add_argument("--classifier-model", help="Registered classifier model ID")
    parser.add_argument("--planner-model", help="Separate registered planner model ID")
    parser.add_argument("--planning", choices=("off", "auto", "always"), default="off",
                        help="Opt-in plans: auto uses a rule for single tasks, always calls the planner")
    parser.add_argument("--plan-file", type=Path, help="Validate and execute a supplied plan; skips classifier/planner calls")
    parser.add_argument("--source", type=Path, help="Bounded UTF-8 source material for planning/drafts")
    parser.add_argument("--constraint", action="append", default=[], help="User constraint (repeatable)")
    parser.add_argument("--durable", action="store_true", help="Persist job transitions in SQLite")
    parser.add_argument("--request-contract", choices=("baseline", "isolated"), default="baseline", help="Opt-in isolated task prompts; baseline preserves frozen requests")
    parser.add_argument('--tools-config',type=Path,help='Explicit real-tool authority; enables durable isolated execution')
    parser.add_argument("--db", type=Path, default=Path(".cortex/jobs.sqlite3"))
    parser.add_argument("--pause-after-step", type=int, help="Durable checkpoint pause after this many finished steps")
    parser.add_argument("--draft-checks", type=Path, help="Typed max_words/required_sections JSON")
    parser.add_argument("--currency", choices=("USD", "CAD", "AUD", "EUR", "GBP", "INR", "JPY", "CNY", "CHF", "KRW"), help="Authoritative task currency; absence means unspecified")
    parser.add_argument("--refine", action="store_true", help="Enable bounded draft repair")
    parser.add_argument("--max-repairs", type=int, choices=(0, 1, 2), help="Hard maximum two repairs; durable/refine defaults to one")
    parser.add_argument("--worker-model", help="Explicit registered worker model ID; cannot bypass eligibility")
    parser.add_argument("--extraction-mode", choices=("parser", "llm", "parser-first"), default="parser")
    parser.add_argument("--recovery-mode", choices=("baseline", "failure-aware"), default="failure-aware")
    parser.add_argument("--fault", choices=FAULTS, default="none")
    parser.add_argument("--max-units", type=int, default=20)
    parser.add_argument("--max-model-calls", type=int, default=12)
    parser.add_argument("--offline", action="store_true", help="Disable every model provider, retain deterministic workers")
    parser.add_argument("--probe-providers", action="store_true", help="Check authenticated model inventories; no generation calls")
    parser.add_argument("--output", type=Path, help="Fresh result/trace filename; never overwritten")
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("Output exists; choose a fresh filename")
    try:
        registry = Registry.load(args.config)
        task_data = json.loads(args.task.read_text(encoding="utf-8")) if args.task else None
        task = Task.from_dict(task_data) if task_data else None
        if args.plan_file and args.plan_file.stat().st_size > 24_000:
            raise ValueError("Plan file exceeds size bound")
        plan = json.loads(args.plan_file.read_text(encoding="utf-8")) if args.plan_file else None
        if args.source and args.source.stat().st_size > 12_000:
            raise ValueError("Source file exceeds size bound")
        source = args.source.read_text(encoding="utf-8") if args.source else ""
        tool_config = None
        if args.tools_config:
            from .tools import ToolRegistry
            tool_config = ToolRegistry(json.loads(args.tools_config.read_text(encoding='utf-8'))).config
            args.durable = True
            args.request_contract = 'isolated'
        policy = None
        checked = args.durable or args.draft_checks or args.refine or args.max_repairs is not None or args.currency is not None
        if checked:
            from .draft_checks import DraftPolicy
            settings = json.loads(args.draft_checks.read_text(encoding="utf-8")) if args.draft_checks else {}
            currency = args.currency or (task_data or {}).get("currency")
            if args.currency and (task_data or {}).get("currency") not in (None, args.currency):
                raise ValueError("Conflicting authoritative currencies")
            policy = DraftPolicy.validate(settings, currency, args.max_repairs if args.max_repairs is not None else 1 if args.durable or args.refine else 0)
            if args.planning == "off":
                args.planning = "auto"
        if args.pause_after_step is not None and not args.durable:
            raise ValueError("Pause requires durable execution")
        if args.durable:
            from .job_store import JobStore
            from .jobs import new_job
            paths = [path for path in (args.config, args.task, args.plan_file, args.source, args.draft_checks,args.tools_config) if path]
            files = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
            options = {"classifier_mode": args.classifier, "classifier_model": args.classifier_model,
                       "planner_model": args.planner_model, "worker_model": args.worker_model,
                       "extraction_mode": args.extraction_mode, "recovery_mode": args.recovery_mode,
                       "fault": args.fault, "offline": args.offline, "probe": args.probe_providers, "planning": args.planning,
                       "request_contract": args.request_contract}
            if tool_config is not None:
                options['tool_config']=tool_config
            store = JobStore(args.db)
            try:
                result = new_job(store, args.request, json.loads(args.config.read_text(encoding="utf-8")),
                                 task_data, plan, args.constraint, source, policy, options,
                                 args.max_units, args.max_model_calls, files, args.pause_after_step)
            finally:
                store.close()
        else:
            result = execute_request(args.request, registry, task, Budget(args.max_units, args.max_model_calls),
                                 args.classifier, args.classifier_model, args.worker_model, args.planner_model,
                                 args.extraction_mode, args.recovery_mode, args.fault, args.offline, args.probe_providers,
                                 args.planning, plan, args.constraint, source, policy, request_contract=args.request_contract)
    except (ValueError, KeyError, TypeError, OSError):
        parser.error("Invalid request, task, budget, model override or configuration; check the setup example")
    result["experiment"] = {"execution": "offline" if args.offline else "runtime",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "python_version": platform.python_version(),
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "task_sha256": hashlib.sha256(args.task.read_bytes()).hexdigest() if args.task else None,
        "plan_sha256": hashlib.sha256(args.plan_file.read_bytes()).hexdigest() if args.plan_file else None,
        "source_material_sha256": hashlib.sha256(args.source.read_bytes()).hexdigest() if args.source else None,
        "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(Path(__file__).parent.glob("*.py"))},
        "arguments": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}}
    rendered = json.dumps(result, indent=2)
    # Defense in depth: credentials must not appear even if a server echoes them.
    variables = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"} | {
        settings.get("api_key_env") for settings in registry.providers.values() if settings.get("api_key_env")}
    for variable in variables:
        secret = os.environ.get(variable)
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]").replace(json.dumps(secret)[1:-1], "[REDACTED]")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered + "\n")
    print(rendered)
    return 0 if result["status"] in ("completed", "no_feasible_supplier", "needs_clarification", "drafted") else 2
