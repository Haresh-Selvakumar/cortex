import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

from .domain import Task
from .engine import run, FAULTS, RECOVERY_MODES
from .extraction import EXTRACTION_MODES, ExtractionRouter
from .policies import (
    Budget,
    LocalPolicy,
    FixedPolicy,
    OllamaPolicy,
    JevPolicy,
)


CODE_CHECKPOINT = "heterogeneous-extraction-v1"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Cortex v1 supplier-analysis governor"
    )

    parser.add_argument(
        "task",
        type=Path,
        help="Task JSON; see examples/",
    )
    parser.add_argument(
        "--policy",
        choices=("local", "fixed", "ollama", "jev"),
        default="local",
    )
    parser.add_argument(
        "--selective",
        action="store_true",
        help="Use rules for sole progress actions; Ollama decisions otherwise",
    )
    parser.add_argument(
        "--recovery-mode",
        choices=RECOVERY_MODES,
        default="baseline",
        help="Optional deterministic failure-aware recovery; baseline preserves selective-v1 behavior",
    )
    parser.add_argument(
        "--model",
        help="Already-installed local Ollama controller model",
    )
    parser.add_argument("--extraction-mode", choices=EXTRACTION_MODES, default="parser",
                        help="Parser only, model only, or structured-parser-first routing")
    parser.add_argument("--extractor-model", help="Already-installed Ollama extraction worker model, configured separately")
    parser.add_argument(
        "--planner-model",
        help="Optional stronger installed Ollama planner",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
    )
    parser.add_argument(
        "--max-units",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=12,
    )
    parser.add_argument(
        "--fault",
        choices=FAULTS,
        default="none",
    )
    parser.add_argument(
        "--output",
        type=Path,
    )

    args = parser.parse_args(argv)

    if args.selective and args.policy != "ollama":
        parser.error("--selective requires --policy ollama")

    if args.recovery_mode == "failure-aware" and not (
        args.policy == "local" or (args.policy == "ollama" and args.selective)
    ):
        parser.error("failure-aware recovery requires --policy local or --policy ollama --selective")

    if args.max_units < 0 or args.max_model_calls < 0:
        parser.error("Budgets must be nonnegative")

    if args.policy == "ollama" and not args.model:
        parser.error("--model is required for Ollama")
    if args.extraction_mode != "parser" and not args.extractor_model:
        parser.error("--extractor-model is required for llm and parser-first extraction")
    if args.extraction_mode == "parser" and args.extractor_model:
        parser.error("--extractor-model requires llm or parser-first extraction")

    if args.output and args.output.exists():
        parser.error("Output already exists; choose a new run filename to preserve results")

    try:
        task = Task.from_dict(
            json.loads(args.task.read_text(encoding="utf-8"))
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error("Invalid task: " + str(exc))

    policy_class = {
        "local": LocalPolicy,
        "fixed": FixedPolicy,
    }.get(args.policy)

    if policy_class:
        policy = policy_class()

    elif args.policy == "jev":
        policy = JevPolicy(
            model=args.model or "jev-1.13.0",
            planner_model=args.planner_model,
            ollama_url=args.ollama_url,
        )

    else:
        policy = OllamaPolicy(
            args.model,
            args.planner_model,
            args.ollama_url,
            selective=args.selective,
            name=(
                "ollama_selective"
                if args.selective
                else "ollama_hybrid"
            ),
        )

    result = run(
        task,
        policy,
        Budget(args.max_units, args.max_model_calls),
        args.fault,
        recovery_mode=args.recovery_mode,
        extraction_mode=args.extraction_mode,
        extraction_worker=ExtractionRouter(args.extraction_mode, args.extractor_model, base_url=args.ollama_url),
    )

    # Content hashes identify the actual code and input, even outside Git.
    result["experiment"] = {
        "code_checkpoint": CODE_CHECKPOINT,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "command": [sys.executable, "-m", "cortex", *(argv if argv is not None else sys.argv[1:])],
        "arguments": {key: str(value) if isinstance(value, Path) else value
                      for key, value in vars(args).items()},
        "task_sha256": hashlib.sha256(args.task.read_bytes()).hexdigest(),
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(__file__).parent.glob("*.py"))
        },
    }

    rendered = json.dumps(result, indent=2)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(rendered + "\n")

    print(rendered)

    return (
        0
        if result["status"] in (
            "completed",
            "no_feasible_supplier",
            "needs_clarification",
        )
        else 2
    )
