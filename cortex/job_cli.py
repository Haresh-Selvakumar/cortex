"""Read-only job inspection and explicit resume; budgets cannot be reset here."""
import argparse
import json
from pathlib import Path
from .job_store import JobStore, JobError
from .jobs import resume_job


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect or resume a durable Cortex job")
    parser.add_argument("action", choices=("inspect", "resume"))
    parser.add_argument("job_id")
    parser.add_argument("--db", type=Path, default=Path(".cortex/jobs.sqlite3"))
    parser.add_argument("--config", type=Path, help="Optional compatible registry for resume")
    parser.add_argument("--retry-interrupted", action="store_true", help="Explicitly authorize replacing an unknown attempt; retains its charge and may duplicate server work")
    parser.add_argument("--pause-after-step", type=int)
    parser.add_argument("--output", type=Path, help="Fresh export file; never overwritten")
    args = parser.parse_args(argv)
    if not args.db.exists():
        parser.error("Job database does not exist")
    if args.output and args.output.exists():
        parser.error("Output exists; choose a fresh filename")
    store = None
    try:
        store = JobStore(args.db)
        if args.action == "inspect":
            if args.config or args.retry_interrupted or args.pause_after_step:
                parser.error("Resume options do not apply to inspection")
            result = store.read(args.job_id)
            result["transitions"] = store.transitions(args.job_id)
        else:
            config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
            result = resume_job(store, args.job_id, args.retry_interrupted, args.pause_after_step, config)
    except (ValueError, OSError, KeyError):
        parser.error("Job unavailable, locked or incompatible; inspect stored state and preserve its inputs/configuration")
    finally:
        if store:
            store.close()
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered + "\n")
    print(rendered)
    return 0 if args.action == "inspect" or result["status"] in ("completed", "drafted", "needs_clarification") else 2
