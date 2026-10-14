"""Save compilation, regression and evidence-audit checks under a fresh name."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a fresh name")
    tempdir = ROOT / ".test_tmp"
    tempdir.mkdir(exist_ok=True)
    environment = dict(os.environ, TMPDIR=str(tempdir))
    checks = []
    for command in (
        ["-m", "compileall", "-q", "cortex", "tests", "scripts"],
        ["-m", "unittest", "discover", "-s", "tests", "-q"],
        ["scripts/verify_selective_baseline.py"],
        ["scripts/verify_extraction_evidence.py"],
    ):
        start = time.perf_counter()
        result = subprocess.run([sys.executable, *command], cwd=ROOT, env=environment,
                                text=True, capture_output=True, timeout=60)
        checks.append({"command": [sys.executable, *command], "exit_code": result.returncode,
                       "stdout": result.stdout, "stderr": result.stderr,
                       "elapsed_seconds": round(time.perf_counter() - start, 3)})
    record = {"recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "all_checks_passed": all(check["exit_code"] == 0 for check in checks),
              "checks": checks,
              "source_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted((ROOT / "cortex").glob("*.py"))}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(record, output, indent=2)
        output.write("\n")
    print(json.dumps({"all_checks_passed": record["all_checks_passed"], "output": str(args.output)}))
    return 0 if record["all_checks_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
