"""Recurring expenses: the date arithmetic, and posting what has come due.

The date maths is where subscriptions go wrong — month ends, leap years, and a
schedule that has been due for a while — so it is pure and tested directly. The
posting tests then check the two properties the hourly loop depends on: nothing
posts twice, and nothing posts before its time.
"""

import os
from datetime import date, timedelta

import pytest
from bson import ObjectId

from app.services import recurring_service


# ─── Pure date arithmetic ───────────────────────────────────────────────


def test_weekly_and_fortnightly_are_just_days():
    assert recurring_service.advance(date(2026, 8, 3), "weekly", 3) == date(2026, 8, 10)
    assert recurring_service.advance(date(2026, 8, 3), "fortnightly", 3) == date(
        2026, 8, 17
    )


def test_monthly_keeps_the_day_of_the_month():
    assert recurring_service.advance(date(2026, 8, 15), "monthly", 15) == date(
        2026, 9, 15
    )


def test_monthly_clamps_into_short_months():
    # Rent due on the 31st is due on the 28th in February. Skipping the month
    # would be worse than being three days early.
    assert recurring_service.advance(date(2026, 1, 31), "monthly", 31) == date(
        2026, 2, 28
    )


def test_a_clamped_schedule_climbs_back_out():
    # The anchor is carried separately from the date, so February does not drag
    # the schedule down to the 28th for the rest of its life.
    february = recurring_service.advance(date(2026, 1, 31), "monthly", 31)
    assert recurring_service.advance(february, "monthly", 31) == date(2026, 3, 31)


def test_quarterly_moves_three_months():
    assert recurring_service.advance(date(2026, 1, 15), "quarterly", 15) == date(
        2026, 4, 15
    )


def test_yearly_handles_the_29th_of_february():
    assert recurring_service.advance(date(2024, 2, 29), "yearly", 29) == date(
        2025, 2, 28
    )
    # And climbs back the next time February is long enough.
    assert recurring_service.advance(date(2027, 2, 28), "yearly", 29) == date(
        2028, 2, 29
    )


def test_nothing_is_due_before_its_time():
    assert (
        recurring_service.due_dates(
            date(2026, 9, 1), "monthly", 1, today=date(2026, 8, 20)
        )
        == []
    )


def test_a_schedule_that_was_missed_catches_up():
    due = recurring_service.due_dates(
        date(2026, 5, 10), "monthly", 10, today=date(2026, 8, 20)
    )
    assert due == [date(2026, 5, 10), date(2026, 6, 10), date(2026, 7, 10), date(2026, 8, 10)]


def test_catch_up_is_bounded():
    # A container that was down for years does not wake up and post hundreds.
    due = recurring_service.due_dates(
        date(2020, 1, 1), "monthly", 1, today=date(2026, 8, 20)
    )
    assert len(due) == recurring_service.MAX_CATCHUP


def test_an_end_date_stops_the_schedule():
    due = recurring_service.due_dates(
        date(2026, 6, 1),
        "monthly",
        1,
        today=date(2026, 12, 1),
        end_date=date(2026, 8, 1),
    )
    assert due == [date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)]


# ─── Posting ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_up_what_we_post():
    """Remove the expenses these tests post.

    They land in the same shared test user as every other suite, and an
    unexpected Netflix charge is exactly the kind of thing that makes another
    module's totals assertion fail an hour later.

    Sync pymongo rather than the app's motor client on purpose: teardown runs
    after the TestClient has closed its event loop, and an async fixture here
    dies with "Event loop is closed".
    """
    yield
    import pymongo

    from app.config import settings

    with pymongo.MongoClient(settings.MONGODB_URI) as mongo:
        db = mongo[os.environ["DB_NAME"]]
        db.expense.delete_many({"recurringId": {"$exists": True}})
        db.recurring_run.delete_many({})
        db.recurring_expense.delete_many({})


@pytest.fixture
def personal_subscription(client):
    """A monthly personal subscription starting today, and its cleanup."""
    created = client.post(
        "/db/recurring",
        json={
            "expenseName": "Netflix",
            "amount": 649,
            "category": "Entertainment",
            "frequency": "monthly",
            "startDate": date.today().isoformat(),
        },
    )
    assert created.status_code == 200, created.text
    schedule = created.json()["data"]
    yield schedule
    client.delete(f"/db/recurring/{schedule['id']}")


def test_a_new_subscription_posts_its_first_occurrence(client, personal_subscription):
    # Creating a subscription that starts today owes its first expense now;
    # making the user wait an hour for it looks broken.
    listed = client.get("/db/recurring").json()
    mine = [r for r in listed["recurring"] if r["id"] == personal_subscription["id"]]
    assert mine and mine[0]["occurrencesPosted"] == 1

    expenses = client.post("/db/expenses/personal", json={}).json()
    rows = expenses if isinstance(expenses, list) else expenses.get("expenses", [])
    assert any(e.get("expenseName") == "Netflix" for e in rows)


def test_posting_twice_does_not_charge_twice(client, personal_subscription):
    # The list endpoint posts what is due before answering, so calling it again
    # is exactly the race the unique index exists to lose safely.
    for _ in range(3):
        client.get("/db/recurring")

    listed = client.get("/db/recurring").json()
    mine = [r for r in listed["recurring"] if r["id"] == personal_subscription["id"]]
    assert mine[0]["occurrencesPosted"] == 1


def test_a_paused_subscription_posts_nothing(client):
    # Started 40 days ago and monthly, so an active schedule would owe two
    # occurrences the moment it exists. Paused, it owes none.
    created = client.post(
        "/db/recurring",
        json={
            "expenseName": "Gym",
            "amount": 1500,
            "frequency": "monthly",
            "startDate": (date.today() - timedelta(days=40)).isoformat(),
            "paused": True,
        },
    ).json()["data"]

    listed = client.get("/db/recurring").json()
    mine = [r for r in listed["recurring"] if r["id"] == created["id"]][0]
    assert mine["occurrencesPosted"] == 0

    # Resuming it catches up the occurrences it missed while paused.
    client.put(f"/db/recurring/{created['id']}", json={"paused": False})
    listed = client.get("/db/recurring").json()
    mine = [r for r in listed["recurring"] if r["id"] == created["id"]][0]
    assert mine["occurrencesPosted"] == 2

    client.delete(f"/db/recurring/{created['id']}")


def test_stopping_a_subscription_keeps_what_it_already_posted(client):
    created = client.post(
        "/db/recurring",
        json={
            "expenseName": "Spotify",
            "amount": 119,
            "frequency": "monthly",
            "startDate": date.today().isoformat(),
        },
    ).json()["data"]

    stopped = client.delete(f"/db/recurring/{created['id']}")
    assert stopped.status_code == 200

    # The schedule is gone...
    assert all(r["id"] != created["id"] for r in client.get("/db/recurring").json()["recurring"])
    # ...but the money was really spent, so the expense stays.
    expenses = client.post("/db/expenses/personal", json={}).json()
    rows = expenses if isinstance(expenses, list) else expenses.get("expenses", [])
    assert any(e.get("expenseName") == "Spotify" for e in rows)


def test_a_future_subscription_posts_nothing_yet(client):
    created = client.post(
        "/db/recurring",
        json={
            "expenseName": "Insurance",
            "amount": 5000,
            "frequency": "yearly",
            "startDate": (date.today() + timedelta(days=30)).isoformat(),
        },
    ).json()["data"]

    listed = client.get("/db/recurring").json()
    mine = [r for r in listed["recurring"] if r["id"] == created["id"]]
    assert mine[0]["occurrencesPosted"] == 0

    client.delete(f"/db/recurring/{created['id']}")


def test_a_group_subscription_charges_the_group(client):
    uid = client.get("/db/user/account").json()["id"]
    event_id = client.post(
        "/db/event",
        json={"eventName": "Flatmates", "users": [uid], "currency": "INR"},
    ).json()["id"]

    created = client.post(
        "/db/recurring",
        json={
            "expenseName": "Broadband",
            "amount": 999,
            "frequency": "monthly",
            "startDate": date.today().isoformat(),
            "type": "group",
            "eventId": event_id,
            "splitMode": "equal",
        },
    )
    assert created.status_code == 200, created.text

    expenses = client.get(f"/db/event/{event_id}/expenses").json()
    rows = expenses if isinstance(expenses, list) else expenses.get("expenses", [])
    assert any(e.get("expenseName") == "Broadband" for e in rows)

    client.delete(f"/db/recurring/{created.json()['data']['id']}")


def test_a_group_subscription_for_a_group_you_are_not_in_is_refused(client):
    # The group exists; the caller is not in it. Knowing an id is not membership.
    from bson import ObjectId as _ObjectId

    import pymongo

    from app.config import settings

    with pymongo.MongoClient(settings.MONGODB_URI) as mongo:
        db = mongo[os.environ["DB_NAME"]]
        event_id = db.event.insert_one(
            {"eventName": "Not Mine", "users": [_ObjectId()], "currency": "INR"}
        ).inserted_id

    response = client.post(
        "/db/recurring",
        json={
            "expenseName": "Sneaky",
            "amount": 100,
            "type": "group",
            "eventId": str(event_id),
        },
    )
    assert response.status_code == 403

    with pymongo.MongoClient(settings.MONGODB_URI) as mongo:
        mongo[os.environ["DB_NAME"]].event.delete_one({"_id": event_id})


# ─── Validation and ownership ───────────────────────────────────────────


def test_a_subscription_needs_a_name_and_an_amount(client):
    assert client.post("/db/recurring", json={"expenseName": "", "amount": 100}).status_code == 400
    assert client.post("/db/recurring", json={"expenseName": "X", "amount": 0}).status_code == 400


def test_an_unknown_frequency_is_refused(client):
    response = client.post(
        "/db/recurring",
        json={"expenseName": "X", "amount": 10, "frequency": "hourly"},
    )
    assert response.status_code == 400


def test_an_end_date_before_the_start_is_refused(client):
    response = client.post(
        "/db/recurring",
        json={
            "expenseName": "X",
            "amount": 10,
            "startDate": "2026-08-01",
            "endDate": "2026-07-01",
        },
    )
    assert response.status_code == 400


def test_a_group_subscription_needs_a_group(client):
    response = client.post(
        "/db/recurring", json={"expenseName": "X", "amount": 10, "type": "group"}
    )
    assert response.status_code == 400


def test_someone_elses_subscription_is_not_found(client):
    response = client.put(f"/db/recurring/{ObjectId()}", json={"paused": True})
    assert response.status_code == 404
