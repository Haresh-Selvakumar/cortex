"""Explicit business contract. Unknown data must never become a zero price."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import csv
import io
import json


class DataError(ValueError):
    pass


class ParsingError(DataError):
    """The fast parser cannot handle this source representation."""

class MissingFieldsError(DataError):
    """Required fields remain missing at this parsing stage."""

    def __init__(self, fields, aliases_checked=False):
        self.fields = tuple(fields)
        self.aliases_checked = aliases_checked
        super().__init__("Missing quote fields: " + ", ".join(fields))

def money(value):
    if isinstance(value, bool):
        raise DataError("Boolean is not a monetary amount")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DataError("Invalid monetary amount") from None
    if not result.is_finite() or result < 0:
        raise DataError("Amounts must be finite and nonnegative")
    return result


def integer(value):
    number = money(value)
    if number != number.to_integral_value():
        raise DataError("Quantity and delivery days must be integers")
    return int(number)


@dataclass(frozen=True)
class Task:
    quantity: int
    budget: Decimal
    deadline_days: int
    source: str

    @classmethod
    def from_dict(cls, data):
        q = integer(data["quantity"])
        if q < 1 or q > 100_000:
            raise DataError("Required quantity must be between 1 and 100000")
        source = data["source"]
        if not isinstance(source, str) or len(source.encode("utf-8")) > 100_000:
            raise DataError("Quote source must be text of at most 100 KB")
        return cls(q, money(data["budget"]), integer(data["deadline_days"]), source)


@dataclass(frozen=True)
class Quote:
    supplier: str
    unit_price: Decimal
    shipping: Decimal
    quantity_available: int
    delivery_days: int


def parse_source(source, robust=False):
    """Strict structured input only; robust mode adds CSV and field aliases.

    Prices are in one task-wide currency, shipping is per order, and tax must
    already be included. No minimum order, tiered pricing, or FX in v1.
    """
    try:
        rows = json.loads(source)
    except json.JSONDecodeError:
        if not robust:
            raise ParsingError("Fast extractor requires a JSON quote array") from None
        rows = list(csv.DictReader(io.StringIO(source)))
    if not isinstance(rows, list) or not rows:
        raise DataError("Expected a nonempty quote array or CSV table")
    aliases = {"name": "supplier", "price": "unit_price", "freight": "shipping",
               "stock": "quantity_available", "lead_days": "delivery_days"}
    required = ("supplier", "unit_price", "shipping", "quantity_available", "delivery_days")
    quotes = []
    for row in rows:
        if not isinstance(row, dict):
            raise DataError("Every quote must be an object")
        row = dict(row)
        if robust:
            for old, new in aliases.items():
                if old in row:
                    if new in row and str(row[old]) != str(row[new]):
                        raise DataError("Conflicting aliases require clarification")
                    row.setdefault(new, row[old])
        missing = [key for key in required if row.get(key) is None or row.get(key) == ""]
        if missing:
            raise MissingFieldsError(missing, aliases_checked=robust)
        name = row["supplier"]
        if not isinstance(name, str) or not name.strip():
            raise DataError("Supplier must be a nonempty string")
        quotes.append(Quote(name.strip(), money(row["unit_price"]), money(row["shipping"]),
                            integer(row["quantity_available"]), integer(row["delivery_days"])))
    if len({q.supplier for q in quotes}) != len(quotes):
        raise DataError("Duplicate supplier identifiers require clarification")
    return quotes


def calculate(task, quotes):
    """Calculation worker: exact decimals, explicit constraint failures."""
    rows = []
    for quote in quotes:
        total = quote.unit_price * task.quantity + quote.shipping
        failures = []
        if quote.quantity_available < task.quantity:
            failures.append("insufficient_stock")
        if quote.delivery_days > task.deadline_days:
            failures.append("late_delivery")
        if total > task.budget:
            failures.append("over_budget")
        rows.append({"supplier": quote.supplier, "total": str(total),
                     "delivery_days": quote.delivery_days, "failures": failures})
    return rows


def verify(task, quotes, rows, reference_quotes=None):
    """Independent checkpoint: source fidelity plus separate arithmetic checks.

    Re-parsing structured source is an integrity check, not proof that the
    source itself is authentic. Does not call calculate() to check its output.
    """
    # Model-derived references must pass the separate extraction/evidence gate.
    # Arithmetic remains independent; this does not prove text interpretation.
    source_quotes = (
        parse_source(task.source, robust=True)
        if reference_quotes is None else reference_quotes
    )
    if quotes != source_quotes:
        return "extraction_mismatch"
    by_name = {row["supplier"]: row for row in rows}
    if len(rows) != len(source_quotes) or len(by_name) != len(rows):
        return "calculation_mismatch"
    for quote in source_quotes:
        row = by_name.get(quote.supplier)
        if row is None:
            return "calculation_mismatch"
        # Equivalent calculation written independently of the worker.
        expected = sum((quote.unit_price for _ in range(task.quantity)), quote.shipping)
        conditions = {"insufficient_stock": quote.quantity_available < task.quantity,
                      "late_delivery": quote.delivery_days > task.deadline_days,
                      "over_budget": expected > task.budget}
        if (money(row["total"]) != expected or row["delivery_days"] != quote.delivery_days
                or set(row["failures"]) != {k for k, failed in conditions.items() if failed}):
            return "calculation_mismatch"
    return None


def recommend(rows):
    eligible = [r for r in rows if not r["failures"]]
    if not eligible:
        return {"status": "no_feasible_supplier", "supplier": None, "total": None,
                "reason": "No quote satisfies quantity, budget and delivery constraints."}
    winner = min(eligible, key=lambda r: (money(r["total"]), r["delivery_days"], r["supplier"]))
    return {"status": "completed", "supplier": winner["supplier"], "total": winner["total"],
            "reason": "Lowest feasible total; ties use earliest delivery, then supplier name."}
