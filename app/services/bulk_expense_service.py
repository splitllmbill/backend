"""One contract for expenses that arrive in bulk, whatever produced them.

Three things want to create a batch of expenses: our own model, a chatbot the
person pasted a prompt into, and a bank statement parsed on their device. They
are all just *producers of the same JSON*, so the schema, the validator and
the preview live here once rather than three times. Our model is then the
convenient path rather than a dependency — when it is down or wrong, a paste
does the same job through the same door.

Nothing here writes: :func:`validate` is pure, so a preview can be exact
about what would happen before anyone commits to it.
"""

from __future__ import annotations

import datetime
import json

from app.services import category_service

#: The row shape every producer targets. Deliberately small — anything the app
#: can work out for itself (who paid, how it splits, the currency) is not asked
#: of a language model, which is where they invent things.
ROW_FIELDS = {
    "name": "string, required — what the expense was for",
    "amount": "number, required — a positive amount, digits only",
    "date": "string, optional — YYYY-MM-DD; today if you leave it out",
    "category": f"string, optional — one of: {', '.join(category_service.CATEGORIES)}",
    "notes": "string, optional — anything worth keeping, otherwise omit",
}

MAX_ROWS = 200


def build_prompt(currency: str = "") -> str:
    """The text a person pastes into whichever chatbot they already use.

    Generated from the same field list the validator enforces, so the two
    cannot drift: change ROW_FIELDS and both the instructions and the checking
    move together.
    """
    fields = "\n".join(f"- {name}: {rule}" for name, rule in ROW_FIELDS.items())
    example = json.dumps(
        {
            "expenses": [
                {
                    "name": "Groceries",
                    "amount": 1240.5,
                    "date": "2026-08-03",
                    "category": "Groceries",
                },
                {"name": "Bus fare", "amount": 60, "date": "2026-08-03"},
            ]
        },
        indent=2,
    )
    return (
        "Read the expenses below and return them as JSON.\n\n"
        "Return ONLY a JSON object, no explanation, no markdown fence. It must\n"
        'have one key, "expenses", whose value is an array of objects with:\n\n'
        f"{fields}\n\n"
        + (f"Amounts are in {currency}. " if currency else "")
        + "Ignore anything that is not an expense — refunds, transfers between\n"
        "your own accounts, and opening or closing balances are not expenses.\n\n"
        "Example of the exact shape:\n"
        f"{example}\n\n"
        "Here is the text:\n"
        "---\n"
    )


def _parse_amount(value) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, "amount must be a number"
    if isinstance(value, (int, float)):
        amount = float(value)
    elif isinstance(value, str):
        # Models like to hand back "1,240.50" or "₹1240".
        cleaned = "".join(c for c in value if c.isdigit() or c in ".-")
        try:
            amount = float(cleaned)
        except ValueError:
            return None, f"amount {value!r} is not a number"
    else:
        return None, "amount must be a number"

    if amount <= 0:
        return None, "amount must be greater than zero"
    if amount > 1_000_000_000:
        return None, "amount is implausibly large"
    return round(amount, 2), None


def _parse_date(value) -> tuple[str, str | None]:
    """Normalise to YYYY-MM-DD, defaulting to today."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    if value in (None, ""):
        return today.isoformat(), None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(text[:11].strip(), fmt).date().isoformat(), None
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat(), None
    except ValueError:
        return today.isoformat(), f"date {value!r} was not understood; using today"


def validate(payload) -> dict:
    """Check a batch without writing anything.

    Returns every row either way — accepted rows carry the expense that would
    be created, rejected rows carry why. A partly-bad batch is normal (a model
    drops a field, a statement line is not an expense), so it is reported
    rather than refused: the person picks.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(_strip_fence(payload))
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"That is not valid JSON ({e.msg})", "rows": []}

    rows = payload.get("expenses") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {
            "ok": False,
            "error": 'Expected an object with an "expenses" array.',
            "rows": [],
        }
    if not rows:
        return {"ok": False, "error": "No expenses found.", "rows": []}
    if len(rows) > MAX_ROWS:
        return {
            "ok": False,
            "error": f"That is {len(rows)} expenses; {MAX_ROWS} at a time is the limit.",
            "rows": [],
        }

    results = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            results.append({"index": index, "ok": False, "errors": ["not an object"]})
            continue

        errors: list[str] = []
        warnings: list[str] = []

        name = str(row.get("name") or row.get("expenseName") or "").strip()
        if not name:
            errors.append("name is required")

        amount, amount_error = _parse_amount(row.get("amount"))
        if amount_error:
            errors.append(amount_error)

        date, date_warning = _parse_date(row.get("date"))
        if date_warning:
            warnings.append(date_warning)

        raw_category = str(row.get("category") or "").strip()
        category = category_service.normalize(raw_category) if raw_category else ""
        if not category:
            # No category given, or one nobody recognises: guess from the title
            # rather than dumping everything into General.
            category = category_service.suggest_from_keywords(name) or "General"
        elif raw_category and category.lower() != raw_category.lower():
            warnings.append(f"category {raw_category!r} recorded as {category!r}")

        if errors:
            results.append({"index": index, "ok": False, "errors": errors, "name": name})
            continue

        results.append({
            "index": index,
            "ok": True,
            "warnings": warnings,
            "expense": {
                "expenseName": name,
                "amount": amount,
                "date": date,
                "category": category,
                "description": str(row.get("notes") or "").strip(),
            },
        })

    return {
        "ok": any(r["ok"] for r in results),
        "acceptedCount": sum(1 for r in results if r["ok"]),
        "rejectedCount": sum(1 for r in results if not r["ok"]),
        "rows": results,
    }


def _strip_fence(text: str) -> str:
    """Drop a ```json fence, which chatbots add however firmly you ask them not to."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -len("```")]
    return body.strip()
