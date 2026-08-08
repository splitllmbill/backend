"""Event totals: where the money went, as opposed to who owes whom.

`paid`, `spend` and `net` answer three different questions that a single
balance number conflates, and settling up must move `net` without ever
touching `spend` — squaring a debt is not the group consuming anything.
"""

import pytest

def _summary_of(client, event_id):
    response = client.get(f"/db/event/{event_id}/summary")
    assert response.status_code == 200, response.text
    return response.json()


def _by_name(summary):
    return {m["name"]: m for m in summary["members"]}


@pytest.fixture
def trip(client):
    """A group with one member plus two placeholders, and a 300 dinner.

    The caller fronts the whole bill and it splits three ways, so the shape is
    the interesting one: one payer, three spenders.
    """
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event",
        json={
            "eventName": "Totals Trip",
            "users": [account["id"], {"name": "Ana"}, {"name": "Bo"}],
            "currency": "INR",
        },
    )
    assert event.status_code == 200, event.text
    event = event.json()

    members = client.get(f"/db/event/{event['id']}/users").json()
    rows = members if isinstance(members, list) else members.get("users", [])
    ids = {m["name"]: m["id"] for m in rows}
    assert "Ana" in ids and "Bo" in ids, f"placeholders missing: {rows}"
    return client, account, event, ids


def test_summary_separates_what_each_member_paid_from_what_they_spent(trip):
    client, account, event, ids = trip
    me = account["id"]
    ana, bo = ids["Ana"], ids["Bo"]

    client.post(
        "/db/expense",
        json={
            "expenseName": "Dinner",
            "amount": 300,
            "eventId": event["id"],
            "type": "group",
            "shares": [
                {"userId": me, "amount": 100},
                {"userId": ana, "amount": 100},
                {"userId": bo, "amount": 100},
            ],
            "payShares": [{"userId": me, "amount": 300}],
        },
    )

    summary = _summary_of(client, event["id"])
    assert summary["groupTotal"] == 300
    assert summary["expenseCount"] == 1
    assert summary["currency"] == "INR"

    members = _by_name(summary)
    # The payer fronted everything but only consumed a third.
    assert members[account["name"]]["paid"] == 300
    assert members[account["name"]]["spend"] == 100
    assert members[account["name"]]["net"] == 200

    assert members["Ana"]["paid"] == 0
    assert members["Ana"]["spend"] == 100
    assert members["Ana"]["net"] == -100

    # Every net cancels out: the group as a whole owes itself nothing.
    assert round(sum(m["net"] for m in summary["members"]), 2) == 0


def test_settling_up_moves_net_without_touching_spend(trip):
    client, account, event, ids = trip
    me = account["id"]
    ana = ids["Ana"]

    client.post(
        "/db/expense",
        json={
            "expenseName": "Dinner",
            "amount": 200,
            "eventId": event["id"],
            "type": "group",
            "shares": [
                {"userId": me, "amount": 100},
                {"userId": ana, "amount": 100},
            ],
            "payShares": [{"userId": me, "amount": 200}],
        },
    )
    before = _by_name(_summary_of(client, event["id"]))
    assert before["Ana"]["net"] == -100

    # Ana squares up: she pays 100 to the account holder.
    client.post(
        "/db/expense",
        json={
            "expenseName": "Settle",
            "amount": 100,
            "eventId": event["id"],
            "type": "settle",
            "shares": [{"userId": me, "amount": 100}],
            "payShares": [{"userId": ana, "amount": 100}],
        },
    )

    summary = _summary_of(client, event["id"])
    after = _by_name(summary)

    # Squaring up is not consumption: spend and the group total are unmoved...
    assert after["Ana"]["spend"] == before["Ana"]["spend"] == 100
    assert summary["groupTotal"] == 200
    assert summary["expenseCount"] == 1
    # ...but nobody is owed anything any more.
    assert after["Ana"]["net"] == 0
    assert after[account["name"]]["net"] == 0
