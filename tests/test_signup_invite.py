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
    so the real chain runs on top of that identity — which is the whole point
    here: invite-code validation and the account-status gate both live in it.

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


@pytest.fixture(autouse=True)
async def clean_second_user():
    """Leave no second user behind, however a test ends."""
    yield
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        await mongo[settings.DB_NAME].users.delete_one({"firebaseUid": SECOND_UID})
    finally:
        mongo.close()


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
                "password": "pw",
                "inviteCode": "NOPE1234",
            },
        )
        assert response.status_code == 400

        # No code at all is recoverable, so it parks a pending account rather
        # than turning the person away.
        response = second_user_client.post(
            "/db/signup",
            json={"email": SECOND_EMAIL, "name": "Second User", "password": "pw"},
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


@pytest.mark.asyncio
async def test_signup_with_a_valid_code_is_active_immediately(second_user_client):
    """The normal path is unchanged: a good code means a working account."""
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.DB_NAME]
    try:
        first_user = await db.users.find_one({"firebaseUid": MOCK_USER_UID})
        invite_code = first_user["friendCode"]

        response = second_user_client.post(
            "/db/signup",
            json={
                "email": SECOND_EMAIL,
                "name": "Second User",
                "password": "pw",
                "inviteCode": invite_code,
            },
        )
        assert response.status_code == 200
        created = response.json()
        assert created["status"] == "active"
        assert created["invitedBy"] == invite_code
        assert created["invitedByUserId"] == str(first_user["_id"])

        await db.users.delete_one({"firebaseUid": SECOND_UID})
    finally:
        mongo.close()


@pytest.mark.asyncio
async def test_a_deactivated_account_cannot_talk_itself_back_in(second_user_client):
    """An admin's decision does not come undone by borrowing a friend's code."""
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.DB_NAME]
    try:
        first_user = await db.users.find_one({"firebaseUid": MOCK_USER_UID})
        invite_code = first_user["friendCode"]

        second_user_client.post(
            "/db/signup",
            json={
                "email": SECOND_EMAIL,
                "name": "Second User",
                "password": "pw",
                "inviteCode": invite_code,
            },
        )
        await db.users.update_one(
            {"firebaseUid": SECOND_UID}, {"$set": {"status": "inactive"}}
        )

        blocked = second_user_client.get("/db/user/events")
        assert blocked.status_code == 403
        assert blocked.headers["x-account-status"] == "inactive"

        # An invite code is not a way back in.
        retry = second_user_client.post(
            "/db/activate", json={"inviteCode": invite_code}
        )
        assert retry.status_code == 403
        assert second_user_client.get("/db/user/events").status_code == 403

        await db.users.delete_one({"firebaseUid": SECOND_UID})
    finally:
        mongo.close()
