import pytest
from fastapi.testclient import TestClient
from motor.motor_asyncio import AsyncIOMotorClient

from app.auth import get_verified_identity
from app.config import settings
from app.main import app
from conftest import MOCK_USER_UID

SECOND_UID = "second_user_uid_456"
SECOND_EMAIL = "second@example.com"


@pytest.fixture
def second_user_client():
    """Client whose token resolves to a brand-new (unregistered) identity.

    Overrides the verify-only dependency, not get_current_user, so the real
    signup path (invite-code validation included) is exercised. Only one
    TestClient may be open at a time — app startup rebinds the global Motor
    client to the newest client's loop.
    """

    async def mock_identity():
        return {"uid": SECOND_UID, "email": SECOND_EMAIL, "name": "Second User"}

    app.dependency_overrides[get_verified_identity] = mock_identity
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_verified_identity, None)


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

        # Without an invite code the signup is rejected...
        response = second_user_client.post(
            "/db/signup",
            json={"email": SECOND_EMAIL, "name": "Second User", "password": "pw"},
        )
        assert response.status_code == 400
        assert "Invite code" in response.json()["detail"]

        # ...and with a bogus one too.
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

        # With the real code the account is created and linked to the inviter.
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
        assert created["firebaseUid"] == SECOND_UID
        assert created["invitedBy"] == invite_code
        assert created["role"] == "user"

        await db.users.delete_one({"firebaseUid": SECOND_UID})
    finally:
        mongo.close()
