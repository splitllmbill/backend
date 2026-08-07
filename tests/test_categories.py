"""Category vocabulary, keyword seeds, and the table that learns from users."""

import pymongo
import pytest

from app.config import settings as app_settings
from app.services import category_service as cs


def test_every_spelling_folds_onto_one_vocabulary():
    # Receipt-scanner output, Splitwise's names and the app's older list all
    # used to be separate vocabularies.
    assert cs.normalize("Dining out") == "Food"
    assert cs.normalize("gas/fuel") == "Transport"
    assert cs.normalize("Medical expenses") == "Health"
    assert cs.normalize("Music") == "Entertainment"
    assert cs.normalize("Other") == "General"
    # Canonical values survive, whatever the casing.
    assert cs.normalize("groceries") == "Groceries"
    # Anything unrecognised lands on the default rather than inventing a value.
    assert cs.normalize("Zorbing") == "General"
    assert cs.normalize("") == "General"
    assert cs.normalize(None) == "General"
    assert set(cs._ALIASES.values()) <= set(cs.CATEGORIES)


def test_keyword_matching_respects_word_boundaries():
    assert cs.suggest_from_keywords("Dinner at Toit") == "Food"
    assert cs.suggest_from_keywords("Blinkit order") == "Groceries"
    assert cs.suggest_from_keywords("OLA to office") == "Transport"
    # "cab" must not fire on "cabbage" — the reason matching is whole-word.
    assert cs.suggest_from_keywords("Cabbage and greens") != "Transport"
    assert cs.suggest_from_keywords("Something unheard of") is None


def test_tokenize_keeps_the_whole_title_and_its_useful_words():
    keys = cs.tokenize("Dinner at the Blue Tokai 2")
    assert keys[0] == "~dinner at the blue tokai 2"  # whole-title key
    # Stopwords, short words and bare numbers carry no signal.
    assert "the" not in keys and "at" not in keys and "2" not in keys
    assert "dinner" in keys and "tokai" in keys
    assert cs.tokenize("  ") == []


@pytest.fixture
def hints_collection():
    """An empty hint table, so these tests do not inherit earlier ones' data."""
    sync_client = pymongo.MongoClient(app_settings.MONGODB_URI)
    hints = sync_client[app_settings.DB_NAME].category_hints
    hints.delete_many({})
    yield hints
    hints.delete_many({})
    sync_client.close()


def _suggest(client, name):
    return client.get(
        "/db/expense/category-suggestion", params={"name": name}
    ).json()


def _add_expense(client, name, category):
    return client.post(
        "/db/expense",
        json={
            "expenseName": name,
            "amount": 100.0,
            "category": category,
            "type": "normal",
        },
    )


def test_suggestions_start_from_the_seed_table(client, hints_collection):
    suggestion = _suggest(client, "Dinner at Toit")
    assert suggestion["category"] == "Food"
    assert suggestion["source"] == "keyword"

    # Nothing known and nothing guessable means "leave their choice alone".
    assert _suggest(client, "Qwerty zxcvb")["category"] is None


def test_the_table_learns_what_users_actually_choose(client, hints_collection):
    # The seed table reads "airport" and says Travel...
    assert cs.suggest_from_keywords("Uber to airport") == "Travel"
    assert _suggest(client, "Uber to airport")["source"] == "keyword"

    # ...but this person files it under Transport, twice.
    assert _add_expense(client, "Uber to airport", "Transport").status_code == 200
    assert _add_expense(client, "Uber to airport", "Transport").status_code == 200

    suggestion = _suggest(client, "Uber to airport")
    assert suggestion["category"] == "Transport"
    assert suggestion["source"] == "you"
    assert suggestion["confidence"] == 1.0

    # A title that only shares a word still benefits from what was learned.
    assert _suggest(client, "Uber home")["category"] == "Transport"


def test_learning_also_builds_a_shared_table(client, hints_collection):
    _add_expense(client, "Kirana Mart run", "Groceries")

    # Both tallies are kept: the person's own, and the one everyone feeds.
    global_hint = hints_collection.find_one({"token": "kirana", "userId": None})
    personal_hint = hints_collection.find_one(
        {"token": "kirana", "userId": {"$ne": None}}
    )
    assert global_hint["counts"]["Groceries"] == 1
    assert personal_hint["counts"]["Groceries"] == 1


def test_a_correction_outvotes_the_earlier_answer(client, hints_collection):
    created = _add_expense(client, "Weekly Zorbing", "Entertainment").json()
    assert _suggest(client, "Weekly Zorbing")["category"] == "Entertainment"

    # Editing an expense is the strongest signal there is, so it counts too.
    response = client.put(
        f"/db/expense/{created['data']['id']}",
        json={
            "expenseName": "Weekly Zorbing",
            "amount": 100.0,
            "category": "Health",
        },
    )
    assert response.status_code == 200, response.text
    for _ in range(2):
        _add_expense(client, "Weekly Zorbing", "Health")

    assert _suggest(client, "Weekly Zorbing")["category"] == "Health"


def test_categories_are_normalized_on_the_way_in(client, hints_collection):
    created = _add_expense(client, "Chai and samosa", "Dining out").json()
    stored = client.get(f"/db/expense/{created['data']['id']}").json()
    assert stored["category"] == "Food"


def test_settlements_do_not_teach_the_table(client, hints_collection):
    account = client.get("/db/user/account").json()
    other = client.post(
        "/db/user/phantom", json={"email": "cat-settle@example.com", "name": "Ann"}
    ).json()
    event = client.post(
        "/db/event",
        json={"eventName": "Cats", "users": [account["id"], other["id"]]},
    ).json()
    client.post(
        "/db/expense",
        json={
            "expenseName": "Settle up",
            "amount": 50.0,
            "category": "settle",
            "type": "settle",
            "eventId": event["id"],
            "shares": [
                {"userId": account["id"], "amount": 50.0, "eventId": event["id"]}
            ],
            "payShares": [{"userId": other["id"], "amount": 50.0}],
        },
    )

    # "Settle up" is not a spending category and must not become one.
    assert hints_collection.find_one({"token": "settle"}) is None
    assert _suggest(client, "Settle up")["category"] is None


def test_the_public_category_list_is_the_canonical_one(client):
    body = client.get("/db/categories").json()
    assert body["categories"] == cs.CATEGORIES
    assert body["default"] == "General"
