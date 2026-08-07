"""Changing a group's currency asks what to do with existing transactions."""

import pymongo
import pytest

from app.config import settings as app_settings
from app.services import fx_service


@pytest.fixture(autouse=True)
def usd_inr_rate():
    sync_client = pymongo.MongoClient(app_settings.MONGODB_URI)
    sync_client[app_settings.DB_NAME].fx_rates.replace_one(
        {"base": "USD", "quote": "INR"},
        {"base": "USD", "quote": "INR", "rate": 80.0, "date": "2020-01-01"},
        upsert=True,
    )
    sync_client.close()
    fx_service._rate_cache.clear()
    yield
    fx_service._rate_cache.clear()


def _event_with_usd_expense(client, name):
    """An INR group holding a single $100 expense (stored as ₹8000)."""
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event",
        json={"eventName": name, "users": [account["id"]], "currency": "INR"},
    ).json()
    expense = client.post(
        "/db/expense",
        json={
            "expenseName": "Flights",
            "amount": 100.0,
            "currency": "USD",
            "type": "group",
            "eventId": event["id"],
            "date": "2023-05-01T00:00:00Z",
            "shares": [
                {"userId": account["id"], "amount": 100.0, "eventId": event["id"]}
            ],
            "payShares": [{"userId": account["id"], "amount": 100.0}],
        },
    ).json()["data"]
    return account, event, expense


def _update(client, account, event, currency, apply_to_expenses=None):
    payload = {
        "id": event["id"],
        "eventName": event["eventName"],
        "users": [account["id"]],
        "currency": currency,
    }
    if apply_to_expenses is not None:
        payload["applyToExpenses"] = apply_to_expenses
    return client.put("/db/event", json=payload)


def test_currency_change_is_refused_until_the_user_chooses(client):
    account, event, _ = _event_with_usd_expense(client, "Ask first")

    impact = client.get(
        f"/db/event/{event['id']}/currency-impact", params={"currency": "USD"}
    ).json()
    assert impact == {
        "fromCurrency": "INR",
        "toCurrency": "USD",
        "expenseCount": 1,
        "requiresChoice": True,
    }

    response = _update(client, account, event, "USD")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "currency_change_requires_choice"
    assert detail["expenseCount"] == 1
    assert detail["fromCurrency"] == "INR"
    assert detail["toCurrency"] == "USD"


def test_declining_leaves_existing_transactions_untouched(client):
    account, event, expense = _event_with_usd_expense(client, "Keep as is")

    response = _update(client, account, event, "USD", apply_to_expenses=False)
    assert response.status_code == 200, response.text
    assert response.json()["currency"] == "USD"
    assert response.json()["reconvertedExpenses"] == 0

    stored = client.get(f"/db/expense/{expense['id']}").json()
    assert stored["baseCurrency"] == "INR"
    assert stored["convertedAmount"] == pytest.approx(8000.0)
    # What was actually paid never changes either way.
    assert stored["currency"] == "USD"
    assert stored["amount"] == pytest.approx(100.0)


def test_accepting_reconverts_every_transaction(client):
    account, event, expense = _event_with_usd_expense(client, "Convert all")

    response = _update(client, account, event, "USD", apply_to_expenses=True)
    assert response.status_code == 200, response.text
    assert response.json()["reconvertedExpenses"] == 1

    stored = client.get(f"/db/expense/{expense['id']}").json()
    assert stored["baseCurrency"] == "USD"
    assert stored["amount"] == pytest.approx(100.0)
    assert stored["convertedAmount"] == pytest.approx(100.0)
    assert stored["shares"][0]["convertedAmount"] == pytest.approx(100.0)


def test_currency_change_needs_no_prompt_on_an_empty_group(client):
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event",
        json={"eventName": "Empty", "users": [account["id"]], "currency": "INR"},
    ).json()

    impact = client.get(
        f"/db/event/{event['id']}/currency-impact", params={"currency": "USD"}
    ).json()
    assert impact["requiresChoice"] is False

    response = _update(client, account, event, "USD")
    assert response.status_code == 200
    assert response.json()["currency"] == "USD"
