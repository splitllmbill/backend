import pytest

from app.database import get_db
from conftest import MOCK_USER_UID


@pytest.mark.asyncio
async def test_link_phantom_expenses_targets_expense_collection():
    """Regression: linking previously wrote to a nonexistent `expenses`."""
    from app.services.user_service import link_phantom_expenses

    db = get_db()
    inserted = await db.expense.insert_one(
        {
            "expenseName": "ghost dinner",
            "type": "friend",
            "shares": [{"email": "ghost@example.com", "userId": None, "amount": 1}],
            "payShares": [],
        }
    )

    await link_phantom_expenses(db, "ghost@example.com", "linked_user_id")

    doc = await db.expense.find_one({"_id": inserted.inserted_id})
    assert doc["shares"][0]["userId"] == "linked_user_id"
    await db.expense.delete_one({"_id": inserted.inserted_id})


@pytest.mark.asyncio
async def test_delete_account_tombstones_user_and_removes_personal_data(client):
    db = get_db()
    user = await db.users.find_one({"firebaseUid": MOCK_USER_UID})
    assert user is not None
    original = dict(user)
    oid = user["_id"]

    personal = await db.expense.insert_one(
        {
            "expenseName": "coffee",
            "amount": 3.0,
            "type": "normal",
            "createdBy": oid,
            "shares": [],
            "payShares": [],
        }
    )
    friend = await db.users.insert_one(
        {
            "firebaseUid": "friend_uid_deletion_test",
            "email": "friend@example.com",
            "name": "Friend",
            "friends": [oid],
        }
    )
    shared = await db.expense.insert_one(
        {
            "expenseName": "dinner",
            "amount": 40.0,
            "type": "friend",
            "createdBy": oid,
            "shares": [
                {"userId": oid, "amount": 20.0},
                {"userId": friend.inserted_id, "amount": 20.0},
            ],
            "payShares": [{"userId": oid, "amount": 40.0}],
        }
    )
    page = await db.payment_page.insert_one({"userId": oid, "upiId": "x@upi"})

    response = client.delete("/db/user/account")
    assert response.status_code == 200

    # Solo data removed; shared expense preserved for the friend's balance.
    assert await db.expense.find_one({"_id": personal.inserted_id}) is None
    assert await db.payment_page.find_one({"_id": page.inserted_id}) is None
    assert await db.expense.find_one({"_id": shared.inserted_id}) is not None

    # Tombstone: every personal field erased, auth id rotated, unfriended.
    tombstone = await db.users.find_one({"_id": oid})
    assert tombstone["name"] == "Deleted User"
    assert tombstone["email"] == ""
    assert tombstone["upiId"] == ""
    assert tombstone["password"] == ""
    assert tombstone["isDeleted"] is True
    assert tombstone["firebaseUid"].startswith("deleted_")
    assert "friendCode" not in tombstone
    friend_doc = await db.users.find_one({"_id": friend.inserted_id})
    assert friend_doc["friends"] == []

    # The deleted uid no longer resolves to an account.
    response = client.delete("/db/user/account")
    assert response.status_code == 404

    # Restore the shared mock user for any later tests.
    await db.users.replace_one({"_id": oid}, original)
    await db.users.delete_one({"_id": friend.inserted_id})
    await db.expense.delete_one({"_id": shared.inserted_id})
