"""Monthly spend trends: personal vs group, and per category.

The definition of "your spend" has to match the homepage summary exactly —
personal expenses count what you *paid*, shared ones count your *share* — or
the dashboard contradicts itself. Settling up is not spending.
"""

import datetime

import pytest

from app.services.dashboard_service import _month_keys, _month_of


def test_month_keys_walk_backwards_across_a_year_boundary():
    keys = _month_keys(4, today=datetime.date(2026, 2, 14))
    assert keys == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_month_keys_always_include_the_current_month_last():
    keys = _month_keys(6, today=datetime.date(2026, 8, 1))
    assert keys[-1] == "2026-08"
    assert len(keys) == 6


def test_month_of_reads_both_datetimes_and_iso_strings():
    assert _month_of(datetime.datetime(2026, 3, 9)) == "2026-03"
    assert _month_of("2026-03-09T10:00:00Z") == "2026-03"
    assert _month_of("2026-03-09") == "2026-03"


def test_month_of_rejects_anything_it_cannot_trust():
    # Better to drop a row from the chart than to bucket it into the wrong month.
    assert _month_of(None) is None
    assert _month_of("") is None
    assert _month_of("09/03/2026") is None


@pytest.mark.asyncio
async def test_trends_split_personal_from_group_and_ignore_settlements(client):
    account = client.get("/db/user/account").json()
    me = account["id"]

    event = client.post(
        "/db/event",
        json={
            "eventName": "Trend Trip",
            "users": [me, {"name": "Cass"}],
            "currency": account.get("defaultCurrency", "INR"),
        },
    ).json()
    members = client.get(f"/db/event/{event['id']}/users").json()
    rows = members if isinstance(members, list) else members.get("users", [])
    cass = next(m["id"] for m in rows if m["name"] == "Cass")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    # The endpoint aggregates everything this account has, and the suite shares
    # one mock user, so measure the delta rather than absolute totals.
    def trends(months=3):
        response = client.get("/db/dashboard/trends", params={"months": months})
        assert response.status_code == 200, response.text
        return response.json()

    def category_total(data, name):
        return next(
            (c["total"] for c in data["categories"] if c["category"] == name), 0
        )

    before = trends()

    # Personal: what you paid.
    client.post("/db/expense", json={
        "expenseName": "Coffee", "amount": 50, "type": "normal",
        "category": "Dining out", "date": today,
        "shares": [{"userId": me, "amount": 50}],
        "payShares": [{"userId": me, "amount": 50}],
    })
    # Group: your share, not the whole bill.
    client.post("/db/expense", json={
        "expenseName": "Taxi", "amount": 200, "eventId": event["id"],
        "type": "group", "category": "Travel", "date": today,
        "shares": [{"userId": me, "amount": 80}, {"userId": cass, "amount": 120}],
        "payShares": [{"userId": me, "amount": 200}],
    })
    # Settling up is not spending and must not appear anywhere.
    client.post("/db/expense", json={
        "expenseName": "Settle", "amount": 120, "eventId": event["id"],
        "type": "settle", "date": today,
        "shares": [{"userId": me, "amount": 120}],
        "payShares": [{"userId": cass, "amount": 120}],
    })

    data = trends()

    assert len(data["months"]) == 3
    assert len(data["personal"]) == len(data["group"]) == 3

    this_month = data["months"][-1]
    assert this_month == datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")

    # 50 paid personally; 80 is your share of the 200 taxi, not the 200. The
    # 120 settlement is spending by nobody and appears in neither series.
    assert data["personal"][-1] - before["personal"][-1] == 50
    assert data["group"][-1] - before["group"][-1] == 80
    assert data["total"][-1] - before["total"][-1] == 130
    assert data["grandTotal"] - before["grandTotal"] == 130

    # "Dining out" was folded onto the canonical vocabulary on write, so the
    # chart groups it with everything else that means the same thing.
    assert category_total(data, "Food") - category_total(before, "Food") == 50
    assert category_total(data, "Travel") - category_total(before, "Travel") == 80
    # Biggest first, so a chart can take the top few and stop.
    assert data["categories"] == sorted(
        data["categories"], key=lambda c: -c["total"]
    )
    # Each series lines up with the month axis.
    assert all(len(c["values"]) == 3 for c in data["categories"])


@pytest.mark.asyncio
async def test_trends_window_is_clamped_to_something_chartable(client):
    assert len(client.get("/db/dashboard/trends", params={"months": 0}).json()["months"]) == 1
    assert len(client.get("/db/dashboard/trends", params={"months": 999}).json()["months"]) == 24
