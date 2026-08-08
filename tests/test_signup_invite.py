import pytest
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

from app.auth import get_account_user, get_current_user, get_verified_identity
from app.config import settings
from app.main import app
from conftest import MOCK_USER_UID

SECOND_UID = "second_user_uid_456"
SECOND_EMAIL = "second@example.com"


@pytest.fixture
def second_user_client():
    """Client whose token resolves to a brand-new (unregistered) identity.

    Only the JWT-decoding dependency is faked. conftest's blanket
    get_current_user / get_account_user overrides are lifted for the duration,
    so the real chain runs on top of that identity — which is the point here:
    invite validation and the account-status gate both live in it.

    Only one TestClient may be open at a time — app startup rebinds the global
    Motor client to the newest client's loop.
    """

    async def mock_identity():
        return {"uid": SECOND_UID, "email": SECOND_EMAIL, "name": "Second User"}

    saved = {
        dep: app.dependency_overrides.pop(dep)
        for dep in (get_current_user, get_account_user)
        if dep in app.dependency_overrides
    }
    app.dependency_overrides[get_verified_identity] = mock_identity
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)
        app.dependency_overrides.update(saved)


@pytest.mark.asyncio
async def test_second_user_signup_requires_and_accepts_invite_code(
    second_user_client,
):
    # The pre-seeded first user's friendCode is the invite code (read with a
    # test-loop-bound connection; see the fixture note about TestClients).
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.DB_NAME]
    try:
        first_user = await db.users.find_one({"firebaseUid": MOCK_USER_UID})
        assert first_user is not None
        invite_code = first_user["friendCode"]
        assert invite_code

        # A bogus code is a typo worth reporting straight away.
        response = second_user_client.post(
            "/db/signup",
            json={
                "email": SECOND_EMAIL,
                "name": "Second User",
                "inviteCode": "NOPE1234",
            },
        )
        assert response.status_code == 400

        # No code at all is recoverable, so it parks a pending account rather
        # than turning the person away.
        response = second_user_client.post(
            "/db/signup",
            json={"email": SECOND_EMAIL, "name": "Second User"},
        )
        assert response.status_code == 200
        created = response.json()
        assert created["status"] == "pending"
        assert created["invitedBy"] is None

        # A pending account can reach nothing.
        blocked = second_user_client.get("/db/user/events")
        assert blocked.status_code == 403
        assert blocked.headers["x-account-status"] == "pending"

        # ...but it can still read its own profile, or it could never be told
        # why it is stuck.
        account = second_user_client.get("/db/user/account")
        assert account.status_code == 200
        assert account.json()["status"] == "pending"

        # A wrong code does not unlock it.
        assert (
            second_user_client.post(
                "/db/activate", json={"inviteCode": "NOPE1234"}
            ).status_code
            == 400
        )

        # The real code does, and records who vouched for them.
        response = second_user_client.post(
            "/db/activate", json={"inviteCode": invite_code}
        )
        assert response.status_code == 200
        activated = response.json()
        assert activated["firebaseUid"] == SECOND_UID
        assert activated["status"] == "active"
        assert activated["invitedBy"] == invite_code
        assert activated["role"] == "user"

        assert second_user_client.get("/db/user/events").status_code == 200

        await db.users.delete_one({"firebaseUid": SECOND_UID})
    finally:
        mongo.close()


THIRD_UID = "third_user_uid_789"
THIRD_EMAIL = "third@example.com"


@pytest.fixture
def third_user_client():
    async def mock_identity():
        return {"uid": THIRD_UID, "email": THIRD_EMAIL, "name": "Third User"}

    app.dependency_overrides[get_verified_identity] = mock_identity
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_verified_identity, None)


@pytest.mark.asyncio
async def test_group_join_code_works_as_a_signup_invite(third_user_client):
    """A group link whose personal invite code got lost still registers the
    visitor — and the referral is credited to the group's creator, so
    `invitedBy` always names a person rather than a group."""
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.DB_NAME]
    try:
        owner = await db.users.find_one({"firebaseUid": MOCK_USER_UID})
        await db.event.insert_one(
            {
                "eventName": "Signup By Link",
                "users": [owner["_id"]],
                "expenses": [],
                "createdBy": owner["_id"],
                "joinCode": "GRPCODE123",
            }
        )

        response = third_user_client.post(
            "/db/signup",
            json={
                "email": THIRD_EMAIL,
                "name": "Third User",
                "inviteCode": "GRPCODE123",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["invitedBy"] == owner["friendCode"]

        await db.users.delete_one({"firebaseUid": THIRD_UID})
        await db.event.delete_one({"joinCode": "GRPCODE123"})
    finally:
        mongo.close()
