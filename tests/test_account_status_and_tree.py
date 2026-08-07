"""Account status, the invite tree, and who disappears when an account goes.

These exercise the service layer directly against Mongo: the shapes involved
(a forest, a tombstone mid-chain) are awkward to set up through HTTP and the
interesting logic is all below the routes anyway.
"""

from contextlib import asynccontextmanager

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

from app.config import settings
from app.services import user_service


def _direct_db():
    """Motor client bound to the *current test's* event loop.

    The app's own client rebinds to each TestClient's internal loop at
    startup, so awaiting it from the test loop raises 'attached to a
    different loop' — direct assertions need their own connection.
    """
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    return client, client[settings.DB_NAME]


async def _make_user(db, name, *, code, inviter=None, **extra):
    doc = {
        "firebaseUid": f"uid_{name}",
        "email": f"{name}@example.com",
        "name": name,
        "friendCode": code,
        "role": "user",
        "status": user_service.STATUS_ACTIVE,
        "invitedBy": inviter["friendCode"] if inviter else None,
        "invitedByUserId": inviter["_id"] if inviter else None,
        **extra,
    }
    doc["_id"] = (await db.users.insert_one(doc)).inserted_id
    return doc


@asynccontextmanager
async def scratch_users():
    """A fresh, isolated slice of the users collection, cleaned up after.

    An async *context manager* rather than a fixture on purpose: pytest-asyncio
    runs fixtures on the session loop here (see pytest.ini), so a Motor client
    opened in one would be torn down against a loop the test no longer owns.
    Opening it inside the test body keeps everything on one loop.
    """
    mongo, db = _direct_db()
    made = []

    async def make(name, **kwargs):
        user = await _make_user(db, name, **kwargs)
        made.append(user["_id"])
        return user

    try:
        yield db, make
    finally:
        await db.users.delete_many({"_id": {"$in": made}})
        mongo.close()


@pytest.mark.asyncio
async def test_tree_nests_invitees_under_whoever_invited_them():
    async with scratch_users() as (db, make):
        root = await make("treeroot", code="TREEROOT")
        child = await make("treechild", code="TREECHLD", inviter=root)
        await make("treegrand", code="TREEGRND", inviter=child)

        forest = await user_service.build_invite_tree(db)
        node = next(n for n in forest if n["name"] == "treeroot")

        assert [c["name"] for c in node["children"]] == ["treechild"]
        assert [g["name"] for g in node["children"][0]["children"]] == ["treegrand"]
        # Direct invitees versus everyone underneath.
        assert node["inviteCount"] == 1
        assert node["totalInvited"] == 2


@pytest.mark.asyncio
async def test_tree_resolves_a_legacy_code_with_no_inviter_id():
    async with scratch_users() as (db, make):
        """Accounts predating invitedByUserId only kept the code."""
        root = await make("legacyroot", code="LEGACYRT")
        await make("legacykid", code="LEGACYKD", inviter=root)
        await db.users.update_one(
            {"friendCode": "LEGACYKD"}, {"$unset": {"invitedByUserId": ""}}
        )

        forest = await user_service.build_invite_tree(db)
        node = next(n for n in forest if n["name"] == "legacyroot")
        assert [c["name"] for c in node["children"]] == ["legacykid"]


@pytest.mark.asyncio
async def test_a_deleted_inviter_does_not_orphan_the_branch():
    async with scratch_users() as (db, make):
        """The tombstone goes; everyone below re-parents to its own inviter."""
        root = await make("gone_root", code="GONEROOT")
        middle = await make("gone_middle", code="GONEMIDL", inviter=root)
        await make("gone_leaf", code="GONELEAF", inviter=middle)

        await db.users.update_one({"_id": middle["_id"]}, {"$set": {"isDeleted": True}})

        forest = await user_service.build_invite_tree(db)
        names = [n["name"] for n in forest]
        assert "gone_middle" not in names

        node = next(n for n in forest if n["name"] == "gone_root")
        # The leaf climbed one level rather than becoming a root of its own.
        assert [c["name"] for c in node["children"]] == ["gone_leaf"]
        assert "gone_leaf" not in names


@pytest.mark.asyncio
async def test_a_placeholder_only_shows_up_if_someone_hangs_off_it():
    async with scratch_users() as (db, make):
        await make("lonely_phantom", code="LONEPHAN", isPhantom=True)
        busy = await make("busy_phantom", code="BUSYPHAN", isPhantom=True)
        await make("phantom_kid", code="PHANKID1", inviter=busy)

        names = [n["name"] for n in await user_service.build_invite_tree(db)]
        assert "lonely_phantom" not in names
        assert "busy_phantom" in names


@pytest.mark.asyncio
async def test_deleted_accounts_drop_out_of_the_invited_list():
    async with scratch_users() as (db, make):
        inviter = await make("listowner", code="LISTOWNR")
        await make("still_here", code="STILLHER", inviter=inviter)
        left = await make("walked_away", code="WALKEDAW", inviter=inviter)

        before = await user_service.get_invited_users(db, "LISTOWNR", inviter["_id"])
        assert {u["name"] for u in before} == {"still_here", "walked_away"}

        await user_service.delete_user_account(db, left["firebaseUid"])

        after = await user_service.get_invited_users(db, "LISTOWNR", inviter["_id"])
        assert {u["name"] for u in after} == {"still_here"}


@pytest.mark.asyncio
async def test_a_gated_account_cannot_vouch_for_anyone():
    async with scratch_users() as (db, make):
        """Pending and deactivated codes stop working as invites."""
        await make("gatekeeper", code="GATEKEEP")

        assert await user_service.resolve_invite_code(db, "GATEKEEP") is not None

        for status in (user_service.STATUS_PENDING, user_service.STATUS_INACTIVE):
            await db.users.update_one(
                {"friendCode": "GATEKEEP"}, {"$set": {"status": status}}
            )
            assert await user_service.resolve_invite_code(db, "GATEKEEP") is None


@pytest.mark.asyncio
async def test_admin_switch_moves_a_pending_account_straight_to_active():
    async with scratch_users() as (db, make):
        user = await make(
            "switchme", code="SWITCHME", status=user_service.STATUS_PENDING
        )

        updated = await user_service.set_account_status(
            db, user["firebaseUid"], user_service.STATUS_INACTIVE
        )
        assert updated["status"] == "inactive"

        updated = await user_service.set_account_status(
            db, user["firebaseUid"], user_service.STATUS_ACTIVE
        )
        assert updated["status"] == "active"


@pytest.mark.asyncio
async def test_signing_in_on_a_placeholder_makes_it_a_real_account():
    async with scratch_users() as (db, make):
        """The person the placeholder stood for turning up is not a new signup."""
        phantom = await make(
            "standin", code="STANDIN1", isPhantom=True, firebaseUid="phantom_standin"
        )

        linked = await user_service.find_or_create_user(
            db, uid="real_standin_uid", email=phantom["email"], name="standin"
        )

        assert linked["status"] == "active"
        assert linked.get("isPhantom") is None
        doc = await db.users.find_one({"_id": phantom["_id"]})
        assert doc["firebaseUid"] == "real_standin_uid"
        assert "isPhantom" not in doc
