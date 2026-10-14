"""Declared, code-enforceable checks. These are not general factual verification."""
from dataclasses import dataclass, asdict
import re
import unicodedata

CURRENCIES = {
    "USD": ("$", "dollar", "dollars"), "CAD": ("$", "dollar", "dollars"),
    "AUD": ("$", "dollar", "dollars"), "EUR": ("€", "euro", "euros"),
    "GBP": ("£", "pound", "pounds", "sterling"), "INR": ("₹", "rupee", "rupees"),
    "JPY": ("¥", "yen"), "CNY": ("¥", "yuan", "renminbi"),
    "CHF": ("franc", "francs"), "KRW": ("₩", "won")}
CHECK_VERSION = "draft-checks-v1"
HARD_REPAIR_LIMIT = 2


@dataclass(frozen=True)
class DraftPolicy:
    max_words: int | None = None
    required_sections: tuple = ()
    currency: str | None = None
    max_repairs: int = 1

    @classmethod
    def validate(cls, settings=None, currency=None, max_repairs=1):
        settings = {} if settings is None else settings
        if type(settings) is not dict or set(settings) - {"max_words", "required_sections"}:
            raise ValueError("Unknown draft check; currency must come from authoritative task input")
        words, sections = settings.get("max_words"), settings.get("required_sections", [])
        if words is not None and (type(words) is not int or not 1 <= words <= 5000):
            raise ValueError("Word limit must be an integer from 1 to 5000")
        if (type(sections) is not list or len(sections) > 8
                or any(type(x) is not str or not x.strip() or len(x.encode()) > 128 or "\n" in x or "\r" in x for x in sections)
                or len({x.strip().casefold() for x in sections}) != len(sections)):
            raise ValueError("Invalid required section labels")
        if currency is not None and (type(currency) is not str or currency not in CURRENCIES):
            raise ValueError("Unsupported authoritative currency code")
        if type(max_repairs) is not int or not 0 <= max_repairs <= HARD_REPAIR_LIMIT:
            raise ValueError("Repair limit must be from zero to two")
        return cls(words, tuple(x.strip() for x in sections), currency, max_repairs)

    def to_dict(self):
        return asdict(self)


def check_draft(text, policy, unchecked=()):
    failures, checked = [], []
    structural = type(text) is str and bool(text.strip()) and len(text.encode()) <= 16_000
    if not structural:
        failures.append({"check": "text_structure", "reason": "Nonempty UTF-8 text of at most 16 KB required."})
        text = text if type(text) is str else ""
    count = len(re.findall(r"\S+", text))
    if policy.max_words is not None:
        checked.append({"check": "max_words", "limit": policy.max_words, "observed": count,
                        "counting_rule": "Every maximal non-whitespace run is one word, including punctuation and code fences."})
        if count > policy.max_words:
            failures.append({"check": "max_words", "limit": policy.max_words, "observed": count})
    for label in policy.required_sections:
        present = any(re.fullmatch(r"(?:#{1,6}\s+)?" + re.escape(label) + r"\s*:?", line.strip(), re.I)
                      for line in text.splitlines())
        checked.append({"check": "required_section", "label": label, "present": present})
        if not present:
            failures.append({"check": "required_section", "label": label})
    allowed = set(CURRENCIES.get(policy.currency, ())) | ({policy.currency} if policy.currency else set())
    labels = set(CURRENCIES) | {item for values in CURRENCIES.values() for item in values if len(item) > 1}
    detected = set(re.findall(r"\b(?:" + "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True)) + r")\b", text, re.I))
    detected.update(char for char in text if unicodedata.category(char) == "Sc")
    disallowed = sorted(item for item in detected if item.casefold() not in {x.casefold() for x in allowed})
    checked.append({"check": "currency_labels", "authoritative_currency": policy.currency,
                    "allowed": sorted(allowed), "detected": sorted(detected),
                    "scope": "Unicode currency symbols and the documented supported currency codes/words"})
    if disallowed:
        failures.append({"check": "currency_labels", "disallowed": disallowed,
                         "authoritative_currency": policy.currency})
    return {"version": CHECK_VERSION, "passed": not failures, "structural_text_passed": structural,
            "checked_constraints": checked, "unchecked_constraints": list(unchecked), "failures": failures,
            "factual_verification": "not_performed", "model_confidence_used": False}


def repair_prompt(original, previous, report, policy):
    import json
    data = json.loads(original["data"])
    data["REPAIR_DATA"] = {"previous_rejected_draft": previous, "specific_check_failures": report["failures"],
                           "declared_policy": policy.to_dict()}
    rendered = json.dumps(data, ensure_ascii=False)
    system = original["system"] + " Repair the prior rejected draft using the specific code-check failures. Return only the repaired draft."
    if len((system + rendered).encode()) > 60_000:
        raise ValueError("Repair context exceeds bound")
    return {**original, "system": system, "data": rendered, "prompt_version": "bounded-draft-repair-v1", "model_role": "draft_repair"}
