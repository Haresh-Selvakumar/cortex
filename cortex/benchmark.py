"""Offline integration benchmark. Not an AI accuracy or token-savings benchmark."""
import argparse
import csv
import io
import json
from pathlib import Path
import random
from statistics import mean

from .domain import Task
from .engine import run
from .policies import Budget, LocalPolicy, FixedPolicy


def fixtures(seed=20261006, count=100):
    rng = random.Random(seed)
    for index in range(count):
        quantity = rng.randint(1, 200)
        budget = rng.randint(200, 30000)
        deadline = rng.randint(1, 6)
        quotes = [{"supplier": "S" + str(j), "unit_price": rng.randint(1, 200),
                   "shipping": rng.randint(0, 3000), "quantity_available": rng.randint(0, 250),
                   "delivery_days": rng.randint(1, 8)} for j in range(rng.randint(2, 6))]
        # Oracle uses the raw generator records and integer arithmetic, not engine workers.
        feasible = [(q["unit_price"] * quantity + q["shipping"], q["delivery_days"], q["supplier"])
                    for q in quotes if q["quantity_available"] >= quantity
                    and q["delivery_days"] <= deadline
                    and q["unit_price"] * quantity + q["shipping"] <= budget]
        winner = min(feasible) if feasible else None
        expected = {"status": "completed" if winner else "no_feasible_supplier",
                    "supplier": winner[2] if winner else None,
                    "total": str(winner[0]) if winner else None}
        for fault in ("none", "extract_timeout", "corrupt_extraction", "bad_arithmetic"):
            task = Task.from_dict({"quantity": quantity, "budget": budget,
                                  "deadline_days": deadline, "source": json.dumps(quotes)})
            yield f"{index}-{fault}", task, fault, expected
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(quotes[0]))
        writer.writeheader()
        writer.writerows(quotes)
        yield f"{index}-csv", Task.from_dict({"quantity": quantity, "budget": budget,
              "deadline_days": deadline, "source": stream.getvalue()}), "none", expected
        incomplete = [dict(quotes[0])]
        del incomplete[0]["shipping"]
        yield f"{index}-missing", Task.from_dict({"quantity": quantity, "budget": budget,
              "deadline_days": deadline, "source": json.dumps(incomplete)}), "none", {
              "status": "needs_clarification", "supplier": None, "total": None}


def benchmark(seed=20261006, count=100):
    records = []
    for label, task, fault, expected in fixtures(seed, count):
        for policy_type in (LocalPolicy, FixedPolicy):
            output = run(task, policy_type(), Budget(), fault)
            actual = output["result"] or {}
            correct = (output["status"] == expected["status"]
                       and actual.get("supplier") == expected["supplier"]
                       and actual.get("total") == expected["total"])
            records.append({"case": label, "policy": output["policy"], "correct": correct,
                            "status": output["status"], "expected": expected,
                            "work_units": output["budget"]["work_units_used"],
                            "latency_ms": output["latency_ms"]})
    summary = {}
    for name in ("local_heuristic", "fixed_workflow"):
        subset = [r for r in records if r["policy"] == name]
        summary[name] = {"cases": len(subset), "correct": sum(r["correct"] for r in subset),
                         "correct_fraction": mean(r["correct"] for r in subset),
                         "mean_work_units": mean(r["work_units"] for r in subset)}
    return {"seed": seed, "base_scenarios": count,
            "scope": "Deterministic integration/fault recovery only; no live models or learned policy.",
            "summary": summary, "records": records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20261006)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("runs/benchmark.json"))
    args = parser.parse_args()
    if not 1 <= args.count <= 10000:
        parser.error("count must be between 1 and 10000")
    report = benchmark(args.seed, args.count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2))


if __name__ == "__main__":
    main()
