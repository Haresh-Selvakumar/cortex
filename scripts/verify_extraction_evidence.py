"""Audit saved development results and preservation without any model calls."""

import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from compare_extraction import aggregate, score


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    with zipfile.ZipFile(ROOT / "snapshots" / "pre_heterogeneous_extraction_v1.zip") as old:
        names = [name for name in old.namelist() if name.startswith("runs/") and not name.endswith("/")]
        assert all((ROOT / name).read_bytes() == old.read(name) for name in names), "Earlier run changed"
        assert (ROOT / "EXPERIMENT_LOG.md").read_bytes().startswith(old.read("EXPERIMENT_LOG.md")), "Earlier log changed"

    suite_path = ROOT / "evaluation" / "extraction_v1" / "suite.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in suite["cases"]}
    checked = 0
    for folder in sorted((ROOT / "runs").glob("heterogeneous_extraction*")):
        if not folder.is_dir():
            continue
        summary = json.loads((folder / "comparison.json").read_text(encoding="utf-8"))
        environment = json.loads((folder / "environment.json").read_text(encoding="utf-8"))
        assert digest(suite_path) == environment["suite_sha256"]
        assert environment["heldout_run"] is False
        assert digest(folder / "runner.py") == environment["runner_sha256"]
        recomputed = []
        for row in summary["records"]:
            result = json.loads((folder / row["file"]).read_text(encoding="utf-8"))
            case = cases[row["case"]]
            fresh = score(case, result)
            assert all(row[key] == value for key, value in fresh.items()), row["file"]
            recomputed.append(fresh)
            budget = result["budget"]
            assert budget["work_units_used"] <= budget["work_units_limit"]
            assert budget["model_calls_used"] <= budget["model_calls_limit"]
            assert budget["model_calls_used"] == len(result["model_usage"])
            assert result["experiment"]["runner_sha256"] == environment["runner_sha256"]
            assert result["experiment"]["source_sha256"]["extraction.py"] == digest(folder / "extraction_worker.py")
            assert result["experiment"]["task_sha256"] == digest(folder / "inputs" / (case["id"] + ".json"))
            for report in result["extraction_reports"]:
                assert report["source_sha256"] == hashlib.sha256(case["task"]["source"].encode()).hexdigest()
            checked += 1
        assert summary["totals"] == aggregate(recomputed), folder.name
    print(json.dumps({"earlier_run_files_unchanged": len(names),
                      "old_experiment_log_prefix_unchanged": True,
                      "saved_development_runs_audited": checked,
                      "heldout_wording_sha256": digest(ROOT / "evaluation" / "extraction_v1" / "heldout_wording.json")}, indent=2))


if __name__ == "__main__":
    main()
