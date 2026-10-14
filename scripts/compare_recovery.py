"""Paired mechanics checks. Mock telemetry is never a live measurement."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import urllib.request
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cortex.cli import main as run_cli


CASES = (
    ("normal", "supplier_task.json", "none", "completed", "Beta", "11500"),
    ("timeout", "supplier_task.json", "extract_timeout", "completed", "Beta", "11500"),
    ("arithmetic", "supplier_task.json", "bad_arithmetic", "completed", "Beta", "11500"),
    ("missing_shipping", "missing_shipping_control.json", "none", "needs_clarification", None, None),
    ("shipping_alias", "shipping_alias_control.json", "none", "completed", "Alpha", "10500"),
)


def mock_response(request, **kwargs):
    payload = json.loads(request.data)
    state = json.loads(payload["messages"][1]["content"])
    action = state["admissible_actions"][0]
    raw = {"message": {"content": json.dumps({"action": action, "reason": "Mock selects first candidate"})},
           "prompt_eval_count": 10, "eval_count": 2}
    return io.BytesIO(json.dumps(raw).encode())


def summarize(case, result, expected, filename, telemetry_kind):
    status, supplier, total = expected
    actual = result["result"] or {}
    trace = result["trace"]
    # These five fixtures have a known bounded repair/clarification route.
    # Calls/planning are avoidable under that oracle; this is not a general metric.
    calls = result["budget"]["model_calls_used"]
    return {
        "case": case,
        "file": filename,
        "recovery_mode": result["configuration"]["recovery_mode"],
        "expected": {"status": status, "supplier": supplier, "total": total},
        "status": result["status"], "supplier": actual.get("supplier"), "total": actual.get("total"),
        "correct": result["status"] == status and actual.get("supplier") == supplier and actual.get("total") == total,
        "synthetic_work_units": result["budget"]["work_units_used"],
        "model_calls": calls,
        "prompt_tokens": result["tokens"]["prompt"], "output_tokens": result["tokens"]["output"],
        "telemetry_kind": telemetry_kind,
        "latency_ms": result["latency_ms"],
        "interventions": {
            "extraction_retries": sum(e["action"] == "retry_extract" for e in trace),
            "flexible_parser_repairs": sum(e["action"] == "repair_extract" for e in trace),
            "recalculations": sum(e["action"] == "repair_calculation" for e in trace),
            "planner_actions": sum(e["action"] == "plan" for e in trace),
            "clarifications": sum(e["action"] == "clarify" for e in trace),
        },
        "unnecessary_interventions_under_fixture_oracle": {
            "avoidable_model_attempts": calls,
            "avoidable_planner_actions": sum(e["action"] == "plan" for e in trace),
            "unnecessary_clarifications": sum(e["action"] == "clarify" for e in trace) if status != "needs_clarification" else 0,
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution", choices=("mocked", "live"), required=True)
    parser.add_argument("--model", default="qwen2.5:0.5b")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("Output directory already exists; choose a new experiment directory")
    args.output_dir.mkdir(parents=True)

    environment = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution": args.execution,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model_requested": args.model,
        "work_unit_limit": 20, "shared_model_call_limit": 4,
        "worker_type": "deterministic Python",
        "request_settings": {"temperature": 0, "num_predict": 256, "think": False,
                             "stream": False, "timeout_seconds": 30},
    }
    if args.execution == "live":
        for endpoint in ("version", "tags"):
            with urllib.request.urlopen("http://localhost:11434/api/" + endpoint, timeout=10) as response:
                environment["ollama_" + endpoint] = json.load(response)
    else:
        environment["telemetry_note"] = "HTTP mocked; 10/2 counts are artificial response fixtures, not real model tokens."
    with (args.output_dir / "environment.json").open("x", encoding="utf-8") as output:
        json.dump(environment, output, indent=2)
        output.write("\n")

    rows = []
    for label, task_file, fault, *expected in CASES:
        for mode in ("baseline", "failure-aware"):
            filename = f"{args.execution}_{mode}_{label}.json"
            output_path = args.output_dir / filename
            cli_args = [str(ROOT / "examples" / task_file), "--policy", "ollama", "--model", args.model,
                        "--selective", "--recovery-mode", mode, "--fault", fault,
                        "--max-units", "20", "--max-model-calls", "4", "--output", str(output_path)]
            mock_context = patch("urllib.request.urlopen", side_effect=mock_response) if args.execution == "mocked" else contextlib.nullcontext()
            with mock_context, contextlib.redirect_stdout(io.StringIO()):
                exit_code = run_cli(cli_args)
            result = json.loads(output_path.read_text(encoding="utf-8"))
            row = summarize(label, result, expected, filename,
                            "artificial_mock_counts" if args.execution == "mocked" else "provider_reported")
            row["exit_code"] = exit_code
            rows.append(row)
            print(json.dumps({key: row[key] for key in ("case", "recovery_mode", "status", "correct", "synthetic_work_units", "model_calls", "prompt_tokens", "output_tokens")}), flush=True)

    summary = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "execution": args.execution,
        "scope": "Five paired supported fixtures, one execution per configuration; deterministic recovery baseline, no learned novelty.",
        "intervention_definition": "For these known fixtures only, model/planner intervention is avoidable because a supported deterministic route exists. Necessary repairs are counted separately.",
        "environment_file": "environment.json", "records": rows,
    }
    with (args.output_dir / "comparison.json").open("x", encoding="utf-8") as output:
        json.dump(summary, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
