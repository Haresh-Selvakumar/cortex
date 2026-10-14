"""Offline checkpoint evidence. Provider examples use mocked HTTP, never real keys."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cortex.product_cli import main as request_main


def response(provider, content):
    text = json.dumps(content)
    if provider == "ollama":
        payload = {"model": "mock-model", "done": True, "done_reason": "stop",
                   "message": {"content": text}, "prompt_eval_count": 12, "eval_count": 4}
    elif provider == "openai":
        payload = {"model": "mock-model", "status": "completed", "id": "resp_mock",
                   "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                   "usage": {"input_tokens": 12, "output_tokens": 4, "input_tokens_details": {"cached_tokens": 0}}}
    else:
        payload = {"model": "mock-model", "stop_reason": "end_turn", "id": "msg_mock",
                   "content": [{"type": "text", "text": text}],
                   "usage": {"input_tokens": 12, "output_tokens": 4,
                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
    return io.BytesIO(json.dumps(payload).encode())


def write(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2)
        output.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="Fresh evidence directory")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    saved = []
    for name, task, fault, units, status in (
        ("normal", "supplier_task.json", "none", 20, "completed"),
        ("timeout", "supplier_task.json", "extract_timeout", 20, "completed"),
        ("persistent_timeout", "supplier_task.json", "extract_timeout_persistent", 2, "retry_limit"),
        ("arithmetic", "supplier_task.json", "bad_arithmetic", 20, "completed"),
        ("missing", "missing_shipping_control.json", "none", 3, "needs_clarification"),
        ("alias", "shipping_alias_control.json", "none", 20, "completed"),
        ("no_budget", "supplier_task.json", "none", 0, "budget_exhausted")):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), patch("urllib.request.urlopen") as http:
            request_main(["Compare suppliers", "--task", str(ROOT / "examples" / task), "--offline",
                          "--fault", fault, "--max-units", str(units), "--max-model-calls", "0"])
        http.assert_not_called()
        result = json.loads(stdout.getvalue())
        assert result["status"] == status, name
        result["experiment"]["evidence_kind"] = "offline_deterministic_with_fault_injection" if fault != "none" else "offline_deterministic"
        write(args.output_dir / (name + ".json"), result)
        saved.append({"file": name + ".json", "status": status, "budget": result["budget"], "tokens": result["tokens"]})

    task_path = ROOT / "examples/supplier_task.json"
    source = json.loads(task_path.read_text())["source"]
    quotes = json.loads(source)
    for quote in quotes:
        quote["issues"] = []
        quote["evidence"] = {field: [source] for field in
                             ("supplier", "unit_price", "shipping", "quantity_available", "delivery_days")}
    proposal = {"task_type": "analysis", "required_capabilities": ["analysis", "supplier_analysis"],
                "missing_essential_information": [], "needs_decomposition": False, "uncertain": False}
    for provider, identifier in (("ollama", "local-worker"), ("openai", "openai-configured"), ("anthropic", "anthropic-configured")):
        stdout = io.StringIO()
        classifier = "local-classifier" if provider == "ollama" else identifier
        # Test-only placeholders; no credential or HTTP request leaves this process.
        env = {"OPENAI_API_KEY": "MOCK-ONLY-KEY", "ANTHROPIC_API_KEY": "MOCK-ONLY-KEY",
               "CORTEX_OPENAI_MODEL": "mock-model", "CORTEX_ANTHROPIC_MODEL": "mock-model"}
        with patch.dict(os.environ, env), contextlib.redirect_stdout(stdout), \
                patch("urllib.request.urlopen", side_effect=[response(provider, proposal), response(provider, {"quotes": quotes})]) as http:
            request_main(["Compare suppliers", "--task", str(task_path), "--classifier", "ai",
                          "--classifier-model", classifier, "--worker-model", identifier, "--max-model-calls", "2"])
        result = json.loads(stdout.getvalue())
        assert http.call_count == result["budget"]["model_calls_used"] == 2
        assert result["status"] == "completed" and result["result"]["total"] == "11500"
        result["experiment"].update(execution="mocked", evidence_kind="mocked_http_contract_and_fixture_extraction",
                                    telemetry="artificial fixture counts, not actual model consumption")
        name = "mocked_" + provider + ".json"
        write(args.output_dir / name, result)
        saved.append({"file": name, "status": result["status"], "budget": result["budget"], "tokens": result["tokens"]})

    with zipfile.ZipFile(ROOT / "snapshots/pre_product_foundation_v1.zip") as old:
        names = [name for name in old.namelist() if name.startswith("runs/") and not name.endswith("/")]
        assert all((ROOT / name).read_bytes() == old.read(name) for name in names), "Previous evidence changed"
        assert (ROOT / "EXPERIMENT_LOG.md").read_bytes().startswith(old.read("EXPERIMENT_LOG.md")), "Old experiment log changed"
    write(args.output_dir / "checks.json", {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
          "checks": saved, "previous_run_files_unchanged": len(names), "old_log_prefix_unchanged": True,
          "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          "limitations": "Provider HTTP and reported tokens in mocked examples are fixtures. No live inference performed."})
    print(json.dumps({"checks_passed": len(saved), "previous_run_files_unchanged": len(names), "output": str(args.output_dir)}, indent=2))


if __name__ == "__main__":
    main()
