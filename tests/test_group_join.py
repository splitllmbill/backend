"""Group join links: previewing, claiming a spot, and signing up from one."""

from contextlib import contextmanager

import pytest

from app.auth import get_current_user, get_verified_identity
from app.main import app
from conftest import mock_get_current_user

JOINER_UID = "join_uid_9001"
JOINER_EMAIL = "joiner@example.com"
SIGNUP_UID = "join_uid_9002"
SIGNUP_EMAIL = "link-signup@example.com"


@contextmanager
def as_identity(uid: str, email: str, name: str):
    """Point the (single) test client at a different signed-in person."""

    async def identity():
        return {"uid": uid, "email": email, "name": name}

    app.dependency_overrides[get_verified_identity] = identity
    app.dependency_overrides[get_current_user] = identity
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_verified_identity, None)
        app.dependency_overrides[get_current_user] = mock_get_current_user


def _register(client, invite_code: str, email: str, name: str):
    response = client.post(
        "/db/signup",
        json={"email": email, "name": name, "password": "pw", "inviteCode": invite_code},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_join_link_is_stable_and_rotatable(client):
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event", json={"eventName": "Link group", "users": [account["id"]]}
    ).json()

    first = client.get(f"/db/event/{event['id']}/join-link").json()
    assert first["code"]
    assert first["url"].endswith(f"/join/{first['code']}")
    # Asking again hands back the same link rather than minting a new one.
    assert client.get(f"/db/event/{event['id']}/join-link").json()["code"] == first["code"]

    rotated = client.post(f"/db/event/{event['id']}/join-link/rotate").json()
    assert rotated["code"] != first["code"]
    # The old link stops working the moment it is rotated.
    assert client.get(f"/db/event/join/{first['code']}").status_code == 404


def test_someone_can_claim_their_spot_from_a_group_link(client):
    account = client.get("/db/user/account").json()
    bob = client.post(
        "/db/user/phantom", json={"email": "bob-claim@example.com", "name": "Bob"}
    ).json()
    event = client.post(
        "/db/event",
        json={
            "eventName": "Goa",
            "users": [account["id"], bob["id"]],
            "currency": "INR",
        },
    ).json()
    # Bob owes me ₹250 while he is still just a placeholder.
    client.post(
        "/db/expense",
        json={
            "expenseName": "Shack",
            "amount": 500.0,
            "currency": "INR",
            "type": "group",
            "eventId": event["id"],
            "shares": [
                {"userId": account["id"], "amount": 250.0, "eventId": event["id"]},
                {"userId": bob["id"], "amount": 250.0, "eventId": event["id"]},
            ],
            "payShares": [{"userId": account["id"], "amount": 500.0}],
        },
    )
    code = client.get(f"/db/event/{event['id']}/join-link").json()["code"]

    with as_identity(JOINER_UID, JOINER_EMAIL, "Joiner"):
        _register(client, account["friendCode"], JOINER_EMAIL, "Joiner")

        preview = client.get(f"/db/event/join/{code}").json()
        assert preview["eventName"] == "Goa"
        assert preview["isMember"] is False
        assert [m["name"] for m in preview["claimableMembers"]] == ["Bob"]

        joined = client.post(
            f"/db/event/join/{code}", json={"claimMemberId": bob["id"]}
        )
        assert joined.status_code == 200, joined.text
        assert joined.json() == {
            "eventId": event["id"],
            "eventName": "Goa",
            "joined": True,
            "claimed": True,
        }

        # The claimed history is now theirs: they owe the ₹250, not "Bob".
        dues = client.get(f"/db/user/event/{event['id']}/dues").json()
        assert dues["totalDebt"] == pytest.approx(250.0)

    # Back as the group owner: the placeholder is gone, replaced by the joiner.
    refreshed = client.get(f"/db/event/{event['id']}").json()
    member_ids = [u["id"] for u in refreshed["users"]]
    assert bob["id"] not in member_ids
    assert len(member_ids) == 2


def test_joining_without_claiming_adds_a_new_member(client):
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event", json={"eventName": "Open house", "users": [account["id"]]}
    ).json()
    code = client.get(f"/db/event/{event['id']}/join-link").json()["code"]

    with as_identity(JOINER_UID, JOINER_EMAIL, "Joiner"):
        _register(client, account["friendCode"], JOINER_EMAIL, "Joiner")

        first = client.post(f"/db/event/join/{code}", json={}).json()
        assert first["joined"] is True
        assert first.get("claimed") is False
        # Following the link again is a no-op, not a duplicate membership.
        second = client.post(f"/db/event/join/{code}", json={}).json()
        assert second["alreadyMember"] is True

    refreshed = client.get(f"/db/event/{event['id']}").json()
    assert len(refreshed["users"]) == 2


def test_a_group_link_also_works_as_a_signup_invite(client):
    account = client.get("/db/user/account").json()
    event = client.post(
        "/db/event", json={"eventName": "Link signup", "users": [account["id"]]}
    ).json()
    code = client.get(f"/db/event/{event['id']}/join-link").json()["code"]

    # Checked before signing up (and, for Google, before the OAuth redirect).
    validated = client.get("/db/invite/validate", params={"code": code}).json()
    assert validated["valid"] is True
    assert validated["type"] == "event"
    assert validated["groupName"] == "Link signup"

    with as_identity(SIGNUP_UID, SIGNUP_EMAIL, "Link Signup"):
        created = _register(client, code, SIGNUP_EMAIL, "Link Signup")
        assert created["invitedBy"] == code

    # Following the link through signup lands them in the group itself.
    refreshed = client.get(f"/db/event/{event['id']}").json()
    assert created["id"] in [u["id"] for u in refreshed["users"]]


def test_invite_validation_rejects_unknown_codes(client):
    account = client.get("/db/user/account").json()

    good = client.get(
        "/db/invite/validate", params={"code": account["friendCode"]}
    ).json()
    assert good["valid"] is True
    assert good["type"] == "friend"

    bad = client.get("/db/invite/validate", params={"code": "NOPE1234"}).json()
    assert bad["valid"] is False
    assert bad["required"] is True

    assert client.get("/db/invite/validate").json()["valid"] is False


@pytest.fixture(autouse=True)
def _cleanup_joiners():
    """Drop the extra accounts so later tests see the usual single user."""
    yield
    import pymongo

    from app.config import settings

    sync_client = pymongo.MongoClient(settings.MONGODB_URI)
    sync_client[settings.DB_NAME].users.delete_many(
        {"firebaseUid": {"$in": [JOINER_UID, SIGNUP_UID]}}
    )
    sync_client.close()
