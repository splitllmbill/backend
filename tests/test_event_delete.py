"""Deleting a group: refused while anyone still owes, allowed once settled."""

import pytest


def _group_with_a_debt(client, name):
    """A group where the other member owes the current user ₹250."""
    account = client.get("/db/user/account").json()
    other = client.post(
        "/db/user/phantom", json={"email": "delete-test@example.com", "name": "Ann"}
    ).json()
    event = client.post(
        "/db/event",
        json={
            "eventName": name,
            "users": [account["id"], other["id"]],
            "currency": "INR",
        },
    ).json()
    client.post(
        "/db/expense",
        json={
            "expenseName": "Taxi",
            "amount": 500.0,
            "currency": "INR",
            "type": "group",
            "eventId": event["id"],
            "shares": [
                {"userId": account["id"], "amount": 250.0, "eventId": event["id"]},
                {"userId": other["id"], "amount": 250.0, "eventId": event["id"]},
            ],
            "payShares": [{"userId": account["id"], "amount": 500.0}],
        },
    )
    return account, other, event


def test_deleting_an_unsettled_group_is_refused_loudly(client):
    account, other, event = _group_with_a_debt(client, "Still owing")

    response = client.delete(f"/db/event/{event['id']}")
    # Used to be a 200 with success:"false", which the app read as success and
    # navigated away from a group that was still there.
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "Ann" in detail
    assert "settle up" in detail.lower()

    # ...and the group really is untouched.
    assert client.get(f"/db/event/{event['id']}").status_code == 200


def test_deleting_a_settled_group_removes_it_and_its_expenses(client):
    account, other, event = _group_with_a_debt(client, "All square")
    client.post(
        "/db/expense",
        json={
            "expenseName": "Settle up",
            "amount": 250.0,
            "currency": "INR",
            "type": "settle",
            "category": "settle",
            "eventId": event["id"],
            "shares": [
                {"userId": account["id"], "amount": 250.0, "eventId": event["id"]}
            ],
            "payShares": [{"userId": other["id"], "amount": 250.0}],
        },
    )
    expense_ids = [
        e["expenseId"]
        for e in client.get(f"/db/event/{event['id']}/expenses").json()["expenses"]
    ]
    assert len(expense_ids) == 2

    response = client.delete(f"/db/event/{event['id']}")
    assert response.status_code == 200, response.text
    assert response.json()["success"] == "true"

    assert client.get(f"/db/event/{event['id']}").status_code == 404
    for expense_id in expense_ids:
        assert client.get(f"/db/expense/{expense_id}").status_code == 404


def test_deleting_an_empty_group_just_works(client):
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event", json={"eventName": "Nothing here", "users": [account["id"]]}
    ).json()

    assert client.delete(f"/db/event/{event['id']}").status_code == 200
    assert client.get(f"/db/event/{event['id']}").status_code == 404


def test_settlements_are_labelled_with_who_paid_whom(client):
    account, other, event = _group_with_a_debt(client, "Named settlement")
    client.post(
        "/db/expense",
        json={
            "expenseName": "Settle up",
            "amount": 250.0,
            "currency": "INR",
            "type": "settle",
            "category": "settle",
            "eventId": event["id"],
            "shares": [
                {"userId": account["id"], "amount": 250.0, "eventId": event["id"]}
            ],
            "payShares": [{"userId": other["id"], "amount": 250.0}],
        },
    )

    expenses = client.get(f"/db/event/{event['id']}/expenses").json()["expenses"]
    settlement = next(e for e in expenses if e["type"] == "settle")
    taxi = next(e for e in expenses if e["type"] == "group")

    # The list can say "Ann paid You" without another round trip.
    assert settlement["paidByName"] == "Ann"
    assert settlement["paidToName"] == "You"
    # Ordinary expenses name their payer too, and have no "paid to".
    assert taxi["paidByName"] == "You"
    assert taxi["paidToName"] == ""
