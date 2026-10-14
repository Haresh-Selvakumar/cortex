"""Development comparison; held-out wording is never loaded or run here."""

import argparse
import contextlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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

FIELDS = ("supplier", "unit_price", "shipping", "quantity_available", "delivery_days")


def mock_reply(case):
    """Ground-truth response fixture for contract checks, not quality evidence."""
    source = case["task"]["source"]
    proposed = []
    for expected in case["expected_quotes"]:
        row = dict(expected)
        if not case["parser_supported"]:
            excerpt = next(block for block in source.split("\n\n")
                           if block.startswith("Supplier " + row["supplier"] + ":"))
        else:
            excerpt = source
        row["evidence"] = {field: [excerpt] if row[field] is not None else [] for field in FIELDS}
        row["issues"] = [{"field": issue["field"], "kind": issue["kind"], "excerpts": [excerpt]}
                         for issue in case.get("expected_issues", [])
                         if issue["supplier"] == row["supplier"]]
        proposed.append(row)
    raw = {"message": {"content": json.dumps({"quotes": proposed})},
           "prompt_eval_count": 20, "eval_count": 4}
    return io.BytesIO(json.dumps(raw).encode())


def same_field(field, actual, expected):
    if actual is None or expected is None:
        return actual is expected
    if field in ("unit_price", "shipping"):
        if type(actual) is not str:
            return False
        try:
            return Decimal(actual) == Decimal(expected)
        except InvalidOperation:
            return False
    return type(actual) is type(expected) and actual == expected


def score(case, result):
    reports = result["extraction_reports"]
    rows = reports[-1].get("quotes", []) if reports else []
    if reports and reports[-1].get("worker_id") == "ollama_extractor":
        # Missing schema keys earn no credit, including expected nulls.
        rows = reports[-1].get("provenance", [])
    rows = [row for row in rows if isinstance(row, dict)]
    by_supplier = {row.get("supplier"): row for row in rows if isinstance(row.get("supplier"), str)}
    matches = 0
    for expected in case["expected_quotes"]:
        actual = by_supplier.get(expected["supplier"], {})
        matches += sum(field in actual and same_field(field, actual[field], expected[field]) for field in FIELDS)
    extra = sum(row.get("supplier") not in {q["supplier"] for q in case["expected_quotes"]} for row in rows)
    denominator = len(case["expected_quotes"]) * len(FIELDS) + extra * len(FIELDS)
    actual_result = result["result"] or {}
    expected_outcome = case["expected_outcome"]
    worker_calls = len(result["worker_model_usage"])
    return {
        "case": case["id"], "kind": case["kind"],
        "mode": result["configuration"]["extraction_mode"],
        "status": result["status"], "supplier": actual_result.get("supplier"), "total": actual_result.get("total"),
        "task_outcome_correct": result["status"] == expected_outcome["status"]
                                and actual_result.get("supplier") == expected_outcome["supplier"]
                                and actual_result.get("total") == expected_outcome["total"],
        "fields_correct": matches, "fields_scored": denominator,
        "field_accuracy": matches / denominator,
        "extra_suppliers": extra,
        "extraction_accepted": bool(reports and reports[-1].get("accepted")),
        "verification_outcomes": [event["verification_outcome"] for event in result["trace"]
                                  if event["action"] == "verify" and "verification_outcome" in event],
        "model_calls": result["budget"]["model_calls_used"],
        "worker_model_calls": worker_calls,
        "unnecessary_worker_calls_under_parser_coverage": worker_calls if case["parser_supported"] else 0,
        "prompt_tokens": result["tokens"]["prompt"], "output_tokens": result["tokens"]["output"],
        "usage_complete": result["tokens"]["usage_complete"],
        "synthetic_work_units": result["budget"]["work_units_used"],
        "engine_latency_ms": result["latency_ms"],
    }


def aggregate(rows):
    totals = {}
    for mode in ("parser", "llm", "parser-first"):
        selected = [row for row in rows if row["mode"] == mode]
        known = all(row["usage_complete"] for row in selected)
        correct = sum(row["fields_correct"] for row in selected)
        count = sum(row["fields_scored"] for row in selected)
        totals[mode] = {
            "cases": len(selected), "task_outcomes_correct": sum(row["task_outcome_correct"] for row in selected),
            "fields_correct": correct, "fields_scored": count, "field_accuracy": correct / count,
            "accepted_extractions": sum(row["extraction_accepted"] for row in selected),
            "model_calls": sum(row["model_calls"] for row in selected),
            "unnecessary_worker_calls_under_parser_coverage": sum(row["unnecessary_worker_calls_under_parser_coverage"] for row in selected),
            "prompt_tokens": sum(row["prompt_tokens"] for row in selected) if known else None,
            "output_tokens": sum(row["output_tokens"] for row in selected) if known else None,
            "usage_complete": known,
            "synthetic_work_units": sum(row["synthetic_work_units"] for row in selected),
            "total_engine_latency_ms": round(sum(row["engine_latency_ms"] for row in selected), 2),
        }
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execution", choices=("mocked", "live"), required=True)
    parser.add_argument("--extractor-model", default="qwen2.5:3b-instruct")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.error("Output directory already exists; choose a new experiment directory")
    suite_path = ROOT / "evaluation" / "extraction_v1" / "suite.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    if suite["split"] != "development":
        parser.error("Only the development split may be run in this checkpoint")
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "inputs").mkdir()
    environment = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "execution": args.execution,
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "suite_sha256": hashlib.sha256(suite_path.read_bytes()).hexdigest(),
        "suite_version": suite["suite_version"], "split": "development", "heldout_run": False,
        "controller": "local deterministic policy", "planner": "local deterministic feasibility probe",
        "extractor_model_requested": args.extractor_model, "work_unit_limit": 20, "shared_model_call_limit": 4,
        "telemetry_kind": "artificial_oracle_response_counts" if args.execution == "mocked" else "provider_reported",
    }
    if args.execution == "live":
        for endpoint in ("version", "tags"):
            with urllib.request.urlopen("http://localhost:11434/api/" + endpoint, timeout=10) as response:
                environment["ollama_" + endpoint] = json.load(response)
    else:
        environment["note"] = "Ground-truth mocked responses test contracts/routing; 20/4 counts are artificial, not model accuracy or real tokens."
    with (args.output_dir / "environment.json").open("x", encoding="utf-8") as output:
        json.dump(environment, output, indent=2)
        output.write("\n")
    rows = []
    for case in suite["cases"]:
        task_path = args.output_dir / "inputs" / (case["id"] + ".json")
        with task_path.open("x", encoding="utf-8") as output:
            json.dump(case["task"], output, indent=2)
            output.write("\n")
        for mode in ("parser", "llm", "parser-first"):
            # Save once, after attaching orchestration provenance to CLI stdout.
            cli_args = [str(task_path), "--policy", "local", "--recovery-mode", "failure-aware",
                        "--extraction-mode", mode, "--max-units", "20", "--max-model-calls", "4"]
            if mode != "parser":
                cli_args.extend(["--extractor-model", args.extractor_model])
            context = patch("urllib.request.urlopen", side_effect=lambda *unused, **kwargs: mock_reply(case)) if args.execution == "mocked" else contextlib.nullcontext()
            rendered = io.StringIO()
            with context, contextlib.redirect_stdout(rendered):
                exit_code = run_cli(cli_args)
            result = json.loads(rendered.getvalue())
            result["experiment"].update(
                execution=args.execution, telemetry_kind=environment["telemetry_kind"],
                runner_sha256=environment["runner_sha256"], suite_sha256=environment["suite_sha256"],
                case_id=case["id"], environment_file="environment.json",
            )
            filename = f"{args.execution}_{mode}_{case['id']}.json"
            with (args.output_dir / filename).open("x", encoding="utf-8") as output:
                json.dump(result, output, indent=2)
                output.write("\n")
            row = score(case, result)
            row.update(file=filename, exit_code=exit_code, telemetry_kind=environment["telemetry_kind"])
            rows.append(row)
            print(json.dumps({key: row[key] for key in ("case", "mode", "status", "task_outcome_correct", "fields_correct", "fields_scored", "worker_model_calls")}), flush=True)
    summary = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(), "execution": args.execution,
        "scope": "Eight small development cases; independent handwritten fields, no training, held-out wording untouched.",
        "unnecessary_call_definition": "Worker calls on structured JSON/CSV cases covered by existing parser, including invalid/missing structured values that require clarification.",
        "field_metric": "Proposed fields, including rejected extractions; exact supplier/int matching and Decimal money equivalence. Missing outputs earn no credit; extra suppliers add five scored errors.",
        "records": rows, "totals": aggregate(rows),
    }
    with (args.output_dir / "comparison.json").open("x", encoding="utf-8") as output:
        json.dump(summary, output, indent=2)
        output.write("\n")
    print(json.dumps(summary["totals"], indent=2))


if __name__ == "__main__":
    main()
