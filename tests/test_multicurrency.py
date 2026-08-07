"""Multi-currency group balances: per-currency ledger + per-member rates."""

import pymongo
import pytest

from app.config import settings as app_settings
from app.services import fx_service
from app.services.event_service import (
    _convert,
    _signed_ledger,
    _viewer_positions,
)

ANN = "aaaaaaaaaaaaaaaaaaaaaaa1"
BOB = "bbbbbbbbbbbbbbbbbbbbbbb2"


def _expense(currency, shares, pay_shares):
    return {
        "currency": currency,
        "shares": [
            {"userId": uid, "originalAmount": amount} for uid, amount in shares
        ],
        "payShares": [
            {"userId": uid, "originalAmount": amount} for uid, amount in pay_shares
        ],
    }


def test_ledger_keeps_each_currency_apart():
    # Ann fronts $100 split evenly; Bob fronts ₹600 split evenly.
    ledger = _signed_ledger(
        [
            _expense("USD", [(ANN, 50), (BOB, 50)], [(ANN, 100)]),
            _expense("INR", [(ANN, 300), (BOB, 300)], [(BOB, 600)]),
        ]
    )
    assert len(ledger) == 1
    (first, second), by_currency = next(iter(ledger.items()))
    assert (first, second) == (ANN, BOB)
    # Positive means the first of the pair owes the second.
    assert by_currency == {"USD": -50.0, "INR": 300.0}

    ann = _viewer_positions(ledger, ANN)[BOB]
    bob = _viewer_positions(ledger, BOB)[ANN]
    assert ann == {"USD": -50.0, "INR": 300.0}
    assert bob == {"USD": 50.0, "INR": -300.0}

    # Converting is the only place a rate is needed, and it nets across
    # currencies: Ann is owed $50 but owes ₹300.
    assert _convert(ann, {"USD": 80.0, "INR": 1.0}) == -3700.0
    # With Ann's own (higher) dollar rate she is owed more.
    assert _convert(ann, {"USD": 100.0, "INR": 1.0}) == -4700.0


def test_ledger_nets_repeated_debts_in_the_same_currency():
    ledger = _signed_ledger(
        [
            _expense("EUR", [(ANN, 30), (BOB, 30)], [(ANN, 60)]),
            _expense("EUR", [(ANN, 20), (BOB, 20)], [(BOB, 40)]),
        ]
    )
    # Bob owes 30, Ann owes 20 -> a single 10 in Bob's column.
    assert ledger[(ANN, BOB)] == {"EUR": -10.0}


def test_ledger_ignores_settled_pairs():
    ledger = _signed_ledger(
        [
            _expense("USD", [(ANN, 25), (BOB, 25)], [(ANN, 50)]),
            _expense("USD", [(ANN, 25), (BOB, 25)], [(BOB, 50)]),
        ]
    )
    assert ledger == {}


@pytest.fixture
def usd_inr_rate():
    """Seed the DB-backed rate source (market USD -> INR = 80)."""
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


def _mixed_currency_event(client):
    """An INR group holding one USD expense and one INR expense."""
    account = client.get("/db/user/account").json()
    other = client.post(
        "/db/user/phantom", json={"email": "mixed-ccy@example.com", "name": "Ann"}
    ).json()
    event = client.post(
        "/db/event",
        json={
            "eventName": "Tokyo trip",
            "users": [account["id"], other["id"]],
            "currency": "INR",
        },
    ).json()

    # I paid $100, split evenly.
    client.post(
        "/db/expense",
        json={
            "expenseName": "Hotel",
            "amount": 100.0,
            "currency": "USD",
            "type": "group",
            "eventId": event["id"],
            "date": "2023-05-01T00:00:00Z",
            "shares": [
                {"userId": account["id"], "amount": 50.0, "eventId": event["id"]},
                {"userId": other["id"], "amount": 50.0, "eventId": event["id"]},
            ],
            "payShares": [{"userId": account["id"], "amount": 100.0}],
        },
    )
    # Ann paid ₹600, split evenly.
    client.post(
        "/db/expense",
        json={
            "expenseName": "Dinner",
            "amount": 600.0,
            "currency": "INR",
            "type": "group",
            "eventId": event["id"],
            "date": "2023-05-02T00:00:00Z",
            "shares": [
                {"userId": account["id"], "amount": 300.0, "eventId": event["id"]},
                {"userId": other["id"], "amount": 300.0, "eventId": event["id"]},
            ],
            "payShares": [{"userId": other["id"], "amount": 600.0}],
        },
    )
    return account, other, event


def test_dues_expose_every_currency_and_follow_member_rates(client, usd_inr_rate):
    account, other, event = _mixed_currency_event(client)

    dues = client.get(f"/db/user/event/{event['id']}/dues").json()
    assert dues["currency"] == "INR"
    assert dues["currencies"] == ["INR", "USD"]
    # Owed $50, owing ₹300 — both shown untouched, no conversion involved.
    assert dues["totalOwedByCurrency"] == {"USD": 50.0}
    assert dues["totalDebtByCurrency"] == {"INR": 300.0}

    # Converted at the market rate the net is 50*80 - 300 = 3700 in my favour.
    assert dues["isOwed"][0]["amount"] == pytest.approx(3700.0)
    assert dues["isOwed"][0]["byCurrency"] == {"USD": -50.0, "INR": 300.0}
    assert dues["rates"]["USD"] == pytest.approx(80.0)

    # My card actually charged me ₹100 to the dollar; say so.
    rates = client.put(
        f"/db/event/{event['id']}/rates", json={"rates": {"USD": 100.0}}
    )
    assert rates.status_code == 200, rates.text
    usd = next(r for r in rates.json()["rates"] if r["currency"] == "USD")
    assert usd["isCustom"] is True
    assert usd["marketRate"] == pytest.approx(80.0)

    dues = client.get(f"/db/user/event/{event['id']}/dues").json()
    assert dues["isOwed"][0]["amount"] == pytest.approx(4700.0)
    # The raw per-currency position is unchanged — only the conversion moved.
    assert dues["totalOwedByCurrency"] == {"USD": 50.0}

    # Clearing the override falls back to the market rate.
    client.put(f"/db/event/{event['id']}/rates", json={"rates": {}})
    dues = client.get(f"/db/user/event/{event['id']}/dues").json()
    assert dues["isOwed"][0]["amount"] == pytest.approx(3700.0)


def test_event_summary_reports_each_member_spend_and_paid(client, usd_inr_rate):
    account, other, event = _mixed_currency_event(client)

    summary = client.get(f"/db/event/{event['id']}/summary").json()
    assert summary["currency"] == "INR"
    assert summary["totalByCurrency"] == {"USD": 100.0, "INR": 600.0}
    assert summary["total"] == pytest.approx(8600.0)
    assert summary["expenseCount"] == 2

    me = next(m for m in summary["members"] if m["id"] == account["id"])
    ann = next(m for m in summary["members"] if m["id"] == other["id"])

    assert me["isYou"] is True
    assert me["paidByCurrency"] == {"USD": 100.0}
    assert me["spendByCurrency"] == {"USD": 50.0, "INR": 300.0}
    assert me["paid"] == pytest.approx(8000.0)
    assert me["spend"] == pytest.approx(4300.0)
    assert me["net"] == pytest.approx(3700.0)

    assert ann["paidByCurrency"] == {"INR": 600.0}
    assert ann["net"] == pytest.approx(-3700.0)
    # Everyone's net nets to zero across the group.
    assert sum(m["net"] for m in summary["members"]) == pytest.approx(0.0)


def test_event_expense_list_keeps_the_currency_it_was_paid_in(client, usd_inr_rate):
    account, other, event = _mixed_currency_event(client)

    listing = client.get(f"/db/event/{event['id']}/expenses").json()
    hotel = next(e for e in listing["expenses"] if e["expenseName"] == "Hotel")

    assert hotel["originalCurrency"] == "USD"
    assert hotel["originalAmount"] == pytest.approx(100.0)
    # ...and the same row also carries the group-currency conversion.
    assert hotel["currency"] == "INR"
    assert hotel["amount"] == pytest.approx(8000.0)
    assert hotel["rate"] == pytest.approx(80.0)
    # The per-expense owe summary is in the expense's own currency.
    assert hotel["user_summary"]["oweCurrency"] == "USD"
    assert hotel["user_summary"]["oweAmount"] == pytest.approx(50.0)
    assert hotel["user_summary"]["oweAmountConverted"] == pytest.approx(4000.0)
    assert hotel["user_summary"]["owePerson"] == "friend"
    assert sorted(listing["currencies"]) == ["INR", "USD"]


def test_settling_up_zeroes_the_net_without_touching_spend(client, usd_inr_rate):
    account, other, event = _mixed_currency_event(client)

    # Ann settles the ₹3700 she owes once everything is converted.
    settle = client.post(
        "/db/expense",
        json={
            "expenseName": "Settle up",
            "amount": 3700.0,
            "currency": "INR",
            "type": "settle",
            "category": "settle",
            "eventId": event["id"],
            "shares": [
                {"userId": account["id"], "amount": 3700.0, "eventId": event["id"]}
            ],
            "payShares": [{"userId": other["id"], "amount": 3700.0}],
        },
    )
    assert settle.status_code == 200, settle.text

    dues = client.get(f"/db/user/event/{event['id']}/dues").json()
    assert dues["totalDebt"] == pytest.approx(0.0)
    assert dues["totalOwed"] == pytest.approx(0.0)

    summary = client.get(f"/db/event/{event['id']}/summary").json()
    me = next(m for m in summary["members"] if m["id"] == account["id"])
    ann = next(m for m in summary["members"] if m["id"] == other["id"])

    # Settling is not spending: the group total and both spends are unchanged.
    assert summary["totalByCurrency"] == {"USD": 100.0, "INR": 600.0}
    assert me["spend"] == pytest.approx(4300.0)
    assert ann["spend"] == pytest.approx(4300.0)
    # ...but it is what finally brings the balances to zero.
    assert me["settled"] == pytest.approx(-3700.0)
    assert ann["settled"] == pytest.approx(3700.0)
    assert me["net"] == pytest.approx(0.0)
    assert ann["net"] == pytest.approx(0.0)
