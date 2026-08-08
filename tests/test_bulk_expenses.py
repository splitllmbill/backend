"""The shared bulk-expense contract.

One schema serves our own model, a chatbot the person pasted a prompt into,
and a statement parsed on their device. The validator is the thing that makes
that safe, so it is tested against what those producers actually return —
fenced JSON, comma'd amounts, half-right rows — not just the happy path.
"""

import json

import pytest

from app.services import bulk_expense_service as bulk


def _rows(payload):
    return bulk.validate(payload)


def test_a_clean_batch_is_accepted():
    result = _rows({"expenses": [
        {"name": "Groceries", "amount": 1240.5, "date": "2026-08-03", "category": "Groceries"},
        {"name": "Bus fare", "amount": 60, "date": "2026-08-03"},
    ]})
    assert result["ok"]
    assert result["acceptedCount"] == 2
    assert result["rejectedCount"] == 0
    assert result["rows"][0]["expense"]["amount"] == 1240.5
    assert result["rows"][0]["expense"]["category"] == "Groceries"


def test_a_markdown_fence_is_tolerated():
    """Chatbots add ```json however firmly the prompt asks them not to."""
    raw = '```json\n{"expenses": [{"name": "Tea", "amount": 30}]}\n```'
    result = _rows(raw)
    assert result["ok"]
    assert result["rows"][0]["expense"]["expenseName"] == "Tea"


def test_amounts_survive_currency_symbols_and_separators():
    result = _rows({"expenses": [{"name": "Rent", "amount": "1,240.50"}]})
    assert result["rows"][0]["expense"]["amount"] == 1240.5


def test_common_date_formats_are_understood():
    for value, expected in [
        ("2026-08-03", "2026-08-03"),
        ("03/08/2026", "2026-08-03"),
        ("03 Aug 2026", "2026-08-03"),
    ]:
        result = _rows({"expenses": [{"name": "X", "amount": 1, "date": value}]})
        assert result["rows"][0]["expense"]["date"] == expected, value


def test_an_unreadable_date_warns_rather_than_failing_the_row():
    # Losing a whole expense over a date format is worse than dating it today
    # and saying so.
    result = _rows({"expenses": [{"name": "X", "amount": 1, "date": "last tuesday"}]})
    row = result["rows"][0]
    assert row["ok"]
    assert any("not understood" in w for w in row["warnings"])


def test_bad_rows_are_reported_individually_not_fatally():
    """A model dropping one field must not cost the other nine expenses."""
    result = _rows({"expenses": [
        {"name": "Fine", "amount": 10},
        {"name": "", "amount": 10},
        {"name": "No amount"},
        {"name": "Negative", "amount": -5},
        "not even an object",
    ]})
    assert result["ok"]
    assert result["acceptedCount"] == 1
    assert result["rejectedCount"] == 4
    assert "name is required" in result["rows"][1]["errors"]
    assert any("greater than zero" in e for e in result["rows"][3]["errors"])


def test_categories_are_folded_onto_the_vocabulary_with_a_note():
    result = _rows({"expenses": [{"name": "Dinner", "amount": 20, "category": "Dining out"}]})
    row = result["rows"][0]
    assert row["expense"]["category"] == "Food"
    assert any("Dining out" in w for w in row["warnings"])


def test_a_missing_category_is_guessed_from_the_title():
    result = _rows({"expenses": [{"name": "Uber to airport", "amount": 400}]})
    assert result["rows"][0]["expense"]["category"] != "General"


def test_junk_input_is_refused_with_a_reason():
    assert not _rows("not json at all")["ok"]
    assert "JSON" in _rows("not json at all")["error"]
    assert not _rows({"expenses": []})["ok"]
    assert not _rows({"nope": 1})["ok"]


def test_an_oversized_batch_is_refused_rather_than_half_imported():
    payload = {"expenses": [{"name": f"x{i}", "amount": 1} for i in range(bulk.MAX_ROWS + 1)]}
    result = _rows(payload)
    assert not result["ok"]
    assert str(bulk.MAX_ROWS) in result["error"]


def test_the_prompt_names_every_field_the_validator_enforces():
    """The prompt is generated from ROW_FIELDS, so the two cannot drift."""
    prompt = bulk.build_prompt(currency="INR")
    for field in bulk.ROW_FIELDS:
        assert field in prompt
    assert "INR" in prompt
    # It must ask for bare JSON, since that is what the validator reads.
    assert "ONLY a JSON object" in prompt
    # And the example must itself satisfy the validator.
    example = json.loads(prompt.split("Example of the exact shape:\n")[1].split("\n\nHere is")[0])
    assert bulk.validate(example)["rejectedCount"] == 0


# ─── Through the API ────────────────────────────────────────────────────


def test_preview_writes_nothing_and_import_writes_everything(client):
    batch = {"payload": {"expenses": [
        {"name": "Bulk coffee", "amount": 45, "category": "Food"},
        {"name": "Bulk bus", "amount": 15},
    ]}}

    before = len(client.post("/db/expenses/personal", json={}).json())

    preview = client.post("/db/expense/bulk/preview", json=batch)
    assert preview.status_code == 200
    assert preview.json()["acceptedCount"] == 2
    # Preview is an opinion, not an action.
    assert len(client.post("/db/expenses/personal", json={}).json()) == before

    created = client.post("/db/expense/bulk", json=batch)
    assert created.status_code == 200, created.text
    assert created.json()["imported"] == 2
    assert created.json()["failed"] == 0
    assert len(client.post("/db/expenses/personal", json={}).json()) == before + 2


def test_import_reports_the_rows_it_could_not_take(client):
    response = client.post("/db/expense/bulk", json={"payload": {"expenses": [
        {"name": "Good one", "amount": 10},
        {"name": "Bad one"},
    ]}})
    assert response.status_code == 200
    assert response.json()["imported"] == 1
    assert response.json()["failed"] == 1


def test_a_wholly_invalid_batch_is_a_400(client):
    response = client.post("/db/expense/bulk", json={"payload": "garbage"})
    assert response.status_code == 400


def test_the_prompt_endpoint_carries_the_users_currency(client):
    response = client.get("/db/expense/bulk/prompt")
    assert response.status_code == 200
    body = response.json()
    assert "expenses" in body["prompt"]
    assert body["maxRows"] == bulk.MAX_ROWS
