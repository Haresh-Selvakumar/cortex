"""Save bounded offline/mocked planning examples and regressions under a fresh directory."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cortex.product_cli import main as request_main


def response(content):
    return io.BytesIO(json.dumps({"model": "mock-model", "done": True, "done_reason": "stop",
        "message": {"content": content if type(content) is str else json.dumps(content)},
        "prompt_eval_count": 20, "eval_count": 10}).encode())


def write(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    cases = [
        ("single", ["Compare suppliers", "--task", "examples/supplier_task.json", "--planning", "auto", "--offline", "--max-model-calls", "0"], [], "completed"),
        ("independent", ["Draft an introduction and a limitation", "--plan-file", "examples/planning_v1/independent_drafts.json", "--max-model-calls", "2"], [response("Cortex chooses workers."), response("Code drafts are not executed.")], "drafted"),
        ("dependency", ["Compare suppliers and draft a recommendation", "--task", "examples/supplier_task.json", "--plan-file", "examples/planning_v1/supplier_then_draft.json", "--max-model-calls", "1"], [response("Draft recommendation: Beta, 11500.")], "drafted"),
        ("source", ["Draft from source", "--plan-file", "examples/planning_v1/source_draft.json", "--source", "examples/planning_v1/source.txt", "--constraint", "Under 30 words", "--max-model-calls", "1"], [response("Cortex selects eligible workers and records verified supplier execution.")], "drafted"),
        ("missing", ["Draft an announcement", "--plan-file", "examples/planning_v1/missing_essential.json", "--offline"], [], "needs_clarification"),
        ("unsupported", ["Create a reminder", "--plan-file", "examples/planning_v1/unsupported_reminder.json", "--offline"], [], "unsupported"),
        ("budget", ["Draft two paragraphs", "--plan-file", "examples/planning_v1/independent_drafts.json", "--max-model-calls", "1"], [response("Only the first draft fits.")], "budget_exhausted"),
        ("invalid_user_plan", ["Draft two paragraphs", "--plan-file", "examples/planning_v1/invalid_cycle.json", "--offline"], [], "invalid_plan"),
        ("planner", ["Compare suppliers", "--task", "examples/supplier_task.json", "--planning", "always", "--max-model-calls", "1"], [response(json.loads((ROOT / "examples/planning_v1/single_supplier.json").read_text()))], "completed"),
        ("invalid_planner", ["Compare suppliers", "--task", "examples/supplier_task.json", "--planning", "always", "--max-model-calls", "1"], [response({"version": 1, "steps": [], "completed": True})], "invalid_plan"),
        ("planner_timeout", ["Compare suppliers", "--task", "examples/supplier_task.json", "--planning", "always", "--max-model-calls", "1"], [TimeoutError("fixture-only")], "planner_failed")]
    summaries = []
    for name, command, replies, status in cases:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("urllib.request.urlopen", side_effect=replies) as http:
            exit_code = request_main(command)
        result = json.loads(output.getvalue())
        assert result["status"] == status, name
        assert result["budget"]["model_calls_used"] == http.call_count == len(replies)
        result["experiment"].update(execution="mocked" if replies else "offline",
                                    evidence_kind="mocked_http" if replies else "offline_deterministic",
                                    telemetry="Artificial 20 input/10 output tokens per successful fixture response; no inference was made." if replies else "No inference calls.")
        write(args.output_dir / (name + ".json"), result)
        summaries.append({"file": name + ".json", "status": status, "cli_exit_code": exit_code,
                          "budget": result["budget"], "tokens": result["tokens"], "latency_ms": result["latency_ms"],
                          "evidence_kind": result["experiment"]["evidence_kind"]})
    tempdir = ROOT / ".test_tmp"
    tempdir.mkdir(exist_ok=True)
    checks = []
    for command in (["-m", "compileall", "-q", "cortex", "tests", "scripts"],
                    ["-m", "unittest", "discover", "-s", "tests", "-q"],
                    ["scripts/verify_selective_baseline.py"], ["scripts/verify_product_foundation_baseline.py"],
                    ["scripts/verify_extraction_evidence.py"]):
        start = time.perf_counter()
        result = subprocess.run([sys.executable, *command], cwd=ROOT, env=dict(os.environ, TMPDIR=str(tempdir)),
                                text=True, capture_output=True, timeout=60)
        checks.append({"command": command, "exit_code": result.returncode, "stdout": result.stdout,
                       "stderr": result.stderr, "elapsed_seconds": round(time.perf_counter() - start, 3)})
    with zipfile.ZipFile(ROOT / "snapshots/pre_structured_planning_v1.zip") as old:
        names = [name for name in old.namelist() if name.startswith("runs/") and not name.endswith("/")]
        assert all((ROOT / name).read_bytes() == old.read(name) for name in names)
        assert (ROOT / "EXPERIMENT_LOG.md").read_bytes().startswith(old.read("EXPERIMENT_LOG.md"))
    record = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(), "all_checks_passed": all(row["exit_code"] == 0 for row in checks),
              "cases": summaries, "checks": checks, "previous_run_files_unchanged": len(names), "previous_log_prefix_unchanged": True,
              "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT / "cortex").glob("*.py"))},
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write(args.output_dir / "checks.json", record)
    print(json.dumps({"all_checks_passed": record["all_checks_passed"], "cases": len(cases), "previous_run_files_unchanged": len(names), "output": str(args.output_dir)}, indent=2))
    return 0 if record["all_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
