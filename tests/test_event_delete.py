"""Deleting a group: refusals must be refusals.

This used to answer HTTP 200 with `success: "false"` — a string, and a truthy
one — so every caller read a refusal as success. The app announced the
deletion and navigated away from a group that was still there.
"""

import pytest


def _group_with_a_debt(client, name):
    """A group where one member owes another, so deletion is refused."""
    account = client.get("/db/user/account").json()
    me = account["id"]
    event = client.post(
        "/db/event",
        json={"eventName": name, "users": [me, {"name": "Robin"}], "currency": "INR"},
    ).json()

    members = client.get(f"/db/event/{event['id']}/users").json()
    rows = members if isinstance(members, list) else members.get("users", [])
    robin = next(m["id"] for m in rows if m["name"] == "Robin")

    client.post("/db/expense", json={
        "expenseName": "Dinner", "amount": 200, "eventId": event["id"],
        "type": "group",
        "shares": [{"userId": me, "amount": 100}, {"userId": robin, "amount": 100}],
        "payShares": [{"userId": me, "amount": 200}],
    })
    return event, me, robin


def test_deleting_a_group_with_debts_is_refused_and_says_why(client):
    event, _, _ = _group_with_a_debt(client, "Owed Money")

    response = client.delete(f"/db/event/{event['id']}")
    assert response.status_code == 409

    detail = response.json()["detail"]
    assert detail["code"] == "event_has_unsettled_dues"
    # Actionable, not just "it failed": who owes whom, and how much.
    assert detail["outstanding"], detail
    assert detail["outstanding"][0]["amount"] == 100
    assert detail["expenseCount"] == 1

    # ...and it is still there.
    assert client.get(f"/db/event/{event['id']}").status_code == 200


def test_the_creator_can_force_past_the_refusal(client):
    event, _, _ = _group_with_a_debt(client, "Force Me")

    refused = client.delete(f"/db/event/{event['id']}")
    assert refused.status_code == 409
    # The refusal tells the client whether offering an override makes sense.
    assert refused.json()["detail"]["canForce"] is True

    forced = client.delete(f"/db/event/{event['id']}", params={"force": "true"})
    assert forced.status_code == 200, forced.text
    assert client.get(f"/db/event/{event['id']}").status_code == 404


def test_a_settled_group_needs_no_override(client):
    account = client.get("/db/user/account").json()
    me = account["id"]
    event = client.post(
        "/db/event",
        json={"eventName": "All Square", "users": [me], "currency": "INR"},
    ).json()
    # Paid entirely by the one person who consumed it: nobody owes anybody.
    client.post("/db/expense", json={
        "expenseName": "Solo", "amount": 40, "eventId": event["id"],
        "type": "group",
        "shares": [{"userId": me, "amount": 40}],
        "payShares": [{"userId": me, "amount": 40}],
    })

    response = client.delete(f"/db/event/{event['id']}")
    assert response.status_code == 200, response.text
    assert client.get(f"/db/event/{event['id']}").status_code == 404


def test_deleting_a_group_that_is_gone_is_a_404_not_a_200(client):
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event",
        json={"eventName": "Twice", "users": [account["id"]], "currency": "INR"},
    ).json()
    assert client.delete(f"/db/event/{event['id']}").status_code == 200
    assert client.delete(f"/db/event/{event['id']}").status_code == 404
