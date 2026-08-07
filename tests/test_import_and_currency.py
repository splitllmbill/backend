import json

import pytest

from app.services import fx_service
from app.services.import_service import parse_splitwise_csv, build_preview

SAMPLE_CSV = """Date,Description,Category,Cost,Currency,Saroja,Adithya Prakash MIT,Sivaganesh B,Sivassri Suresh

2023-03-07,Cab sai house,Taxi,500.00,INR,-100.00,350.00,-100.00,-150.00
2023-03-07,Lunch,Dining out,394.00,INR,0.00,-197.00,394.00,-197.00
2023-03-09,Concert Bus,Music,240.00,INR,180.00,-60.00,-60.00,-60.00
2023-03-10,Sivassri S. paid Adithya P.,Payment,428.66,INR,0.00,-428.66,0.00,428.66

2026-07-15,Total balance, , ,INR,80.00,-335.66,234.00,21.66
"""


def test_parse_splitwise_csv():
    parsed = parse_splitwise_csv(SAMPLE_CSV)
    assert parsed["members"] == [
        "Saroja",
        "Adithya Prakash MIT",
        "Sivaganesh B",
        "Sivassri Suresh",
    ]
    # Total balance row is skipped
    assert len(parsed["rows"]) == 4
    assert parsed["rows"][0]["cost"] == 500.0
    assert parsed["rows"][0]["nets"]["Adithya Prakash MIT"] == 350.0
    # Every row's nets sum to ~0
    for row in parsed["rows"]:
        assert abs(sum(row["nets"].values())) < 0.02


def test_build_preview():
    parsed = parse_splitwise_csv(SAMPLE_CSV)
    preview = build_preview("saiv-will_2026-07-15_export.csv", parsed)
    assert preview["rowCount"] == 4
    assert preview["currencies"] == ["INR"]
    assert preview["paymentRows"] == 1
    assert preview["suggestedEventName"] == "Saiv Will"


def test_import_splitwise_end_to_end(client):
    # Current (seeded) user plays Sivaganesh B; others become phantoms
    account = client.get("/db/user/account").json()
    mapping = {
        "Saroja": {"email": "saroja@example.com"},
        "Adithya Prakash MIT": {"email": "adithya@example.com"},
        "Sivaganesh B": {"userId": account["id"]},
        "Sivassri Suresh": {"email": "sivassri@example.com"},
    }

    response = client.post(
        "/db/import/splitwise",
        files={"file": ("saiv-will_2026-07-15_export.csv", SAMPLE_CSV, "text/csv")},
        data={
            "eventName": "Saiv Will",
            "currency": "INR",
            "mapping": json.dumps(mapping),
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["imported"] == 4
    assert result["skipped"] == []

    event_id = result["eventId"]
    event = client.get(f"/db/event/{event_id}").json()
    assert event["currency"] == "INR"
    assert len(event["users"]) == 4

    # Reconstructed dues must match Splitwise's own "Total balance" row:
    # the seeded user (Sivaganesh B) is owed exactly 234.00 overall.
    dues = client.get(f"/db/user/event/{event_id}/dues").json()
    net = dues["totalOwed"] - dues["totalDebt"]
    assert abs(net - 234.00) < 0.05
    assert dues["currency"] == "INR"


def test_expense_currency_conversion(client):
    # Seed an FX rate directly into Mongo (the fallback source) and confirm a
    # USD expense in an INR event is stored converted at that rate.
    import pymongo

    from app.config import settings as app_settings

    sync_client = pymongo.MongoClient(app_settings.MONGODB_URI)
    sync_client[app_settings.DB_NAME].fx_rates.replace_one(
        {"base": "USD", "quote": "INR"},
        {"base": "USD", "quote": "INR", "rate": 80.0, "date": "2020-01-01"},
        upsert=True,
    )
    sync_client.close()
    fx_service._rate_cache.clear()

    account = client.get("/db/user/account").json()

    event = client.post(
        "/db/event",
        json={"eventName": "US Trip", "users": [account["id"]], "currency": "INR"},
    ).json()

    response = client.post(
        "/db/expense",
        json={
            "expenseName": "Dinner",
            "amount": 10.0,
            "currency": "USD",
            "type": "group",
            "eventId": event["id"],
            "date": "2023-05-01T00:00:00Z",
            "shares": [
                {"userId": account["id"], "amount": 10.0, "eventId": event["id"]}
            ],
            "payShares": [{"userId": account["id"], "amount": 10.0}],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["currency"] == "USD"
    assert data["baseCurrency"] == "INR"
    assert data["originalAmount"] == 10.0
    assert data["fxRate"] == pytest.approx(80.0)
    assert data["amount"] == pytest.approx(800.0)


# ─── Delta imports ──────────────────────────────────────────────────────

EXTRA_ROW = "2023-03-12,Breakfast,Dining out,100.00,INR,-25.00,-25.00,75.00,-25.00"
EXTENDED_CSV = SAMPLE_CSV.replace(
    "\n2026-07-15,Total balance", f"\n{EXTRA_ROW}\n2026-07-15,Total balance"
)


def _mapping(client):
    account = client.get("/db/user/account").json()
    return {
        "Saroja": {"email": "saroja@example.com"},
        "Adithya Prakash MIT": {"email": "adithya@example.com"},
        "Sivaganesh B": {"userId": account["id"]},
        "Sivassri Suresh": {"email": "sivassri@example.com"},
    }


def _import(client, csv_text, **form):
    return client.post(
        "/db/import/splitwise",
        files={"file": ("group_export.csv", csv_text, "text/csv")},
        data={"mapping": json.dumps(_mapping(client)), **form},
    )


def test_reimporting_into_an_event_only_adds_the_delta(client):
    created = _import(client, SAMPLE_CSV, eventName="Delta Trip", currency="INR")
    assert created.status_code == 200, created.text
    event_id = created.json()["eventId"]
    assert created.json()["imported"] == 4

    # The same export again is entirely duplicates — nothing gets added twice.
    again = _import(client, SAMPLE_CSV, eventId=event_id)
    assert again.status_code == 200, again.text
    assert again.json()["imported"] == 0
    assert again.json()["duplicates"] == 4
    assert again.json()["eventId"] == event_id

    # A later export with one more expense brings in exactly that row.
    topped_up = _import(client, EXTENDED_CSV, eventId=event_id)
    assert topped_up.status_code == 200, topped_up.text
    assert topped_up.json()["imported"] == 1
    assert topped_up.json()["duplicates"] == 4

    listing = client.get(f"/db/event/{event_id}/expenses").json()
    assert len(listing["expenses"]) == 5
    assert [e for e in listing["expenses"] if e["expenseName"] == "Breakfast"]


def test_preview_reports_how_much_of_a_file_is_new(client):
    created = _import(client, SAMPLE_CSV, eventName="Preview Trip", currency="INR")
    event_id = created.json()["eventId"]

    preview = client.post(
        "/db/import/splitwise/preview",
        files={"file": ("group_export.csv", EXTENDED_CSV, "text/csv")},
        data={"eventId": event_id},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["rowCount"] == 5
    assert body["newRowCount"] == 1
    assert body["duplicateRowCount"] == 4
    # Existing members come back so the mapping UI can pre-fill itself.
    assert len(body["eventMembers"]) == 4


def test_preview_without_an_event_treats_everything_as_new(client):
    preview = client.post(
        "/db/import/splitwise/preview",
        files={"file": ("group_export.csv", SAMPLE_CSV, "text/csv")},
    ).json()
    assert preview["newRowCount"] == preview["rowCount"] == 4
    assert preview["duplicateRowCount"] == 0
