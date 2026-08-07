import random
import string
from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument
from argon2 import PasswordHasher
from fastapi import HTTPException

from app.config import settings

ph = PasswordHasher()

# ─── Account status ─────────────────────────────────────────────────────
# ACTIVE accounts work normally. INACTIVE ones were switched off by an admin
# and can do nothing until an admin switches them back on. PENDING ones signed
# in but never supplied an invite code: they exist so an admin can see them,
# and they unlock themselves by redeeming a code (see activate_with_invite).
#
# Accounts written before this field existed have no "status" at all, which
# reads as ACTIVE — see account_status(). Phantom placeholders are deliberately
# left ACTIVE too: nobody signs in *as* a phantom, but the real person does
# sign up on that same document (find_or_create_user matches them by email),
# and a gated status there would lock out someone holding a valid invite code.
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_PENDING = "pending"
ACCOUNT_STATUSES = (STATUS_ACTIVE, STATUS_INACTIVE, STATUS_PENDING)


def account_status(user: dict | None) -> str:
    """The account's status, reading accounts that predate the field as active."""
    status = (user or {}).get("status")
    return status if status in ACCOUNT_STATUSES else STATUS_ACTIVE


def with_status(doc: dict | None) -> dict | None:
    """A serialized user with ``status`` guaranteed present."""
    if doc is None:
        return None
    doc["status"] = account_status(doc)
    return doc


def generate_friend_code(length: int = 8) -> str:
    """Generate a unique alphanumeric friend code."""
    num_digits = 2
    num_letters = length - num_digits
    digits = "".join(random.choice(string.digits) for _ in range(num_digits))
    letters = "".join(
        random.choice(string.ascii_uppercase) for _ in range(num_letters)
    )
    code_list = list(digits + letters)
    random.shuffle(code_list)
    return "".join(code_list)


def _serialize_value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v) for k, v in value.items()}
    return value


def serialize_doc(doc: dict) -> dict:
    """Convert MongoDB document to JSON-serializable dict (recursively)."""
    if doc is None:
        return None
    result = {}
    for key, value in doc.items():
        if key == "_id":
            result["id"] = str(value)
        else:
            result[key] = _serialize_value(value)
    return result


async def find_or_create_user(
    db: AsyncIOMotorDatabase, uid: str, email: str, name: str = "", password: str = "", invite_code: str = "",
    allow_pending: bool = False,
) -> dict:
    """Find user by Firebase UID, or create one if not exists.

    ``allow_pending`` lets the login/signup routes park someone who turned up
    without an invite code in a PENDING account rather than refusing them
    outright: the account exists (so an admin can see it) but unlocks nothing
    until a code is redeemed. Every other caller still gets the hard 400, so a
    bare API call cannot conjure accounts.
    """
    user = await db.users.find_one({"firebaseUid": uid})
    if user:
        return with_status(serialize_doc(user))

    # Also check by email for backward compatibility
    user = await db.users.find_one({"email": email})
    if user:
        # Ensure legacy users get a friend code
        if not user.get("friendCode"):
            friend_code = generate_friend_code(8)
            while await db.users.find_one({"friendCode": friend_code}):
                friend_code = generate_friend_code(8)
            await db.users.update_one(
                {"_id": user["_id"]}, 
                {"$set": {"firebaseUid": uid, "friendCode": friend_code}}
            )
            user["firebaseUid"] = uid
            user["friendCode"] = friend_code
        else:
            # Link Firebase UID to existing user
            await db.users.update_one(
                {"_id": user["_id"]}, {"$set": {"firebaseUid": uid}}
            )
            user["firebaseUid"] = uid

        # Someone signing in on a phantom's document *is* that person arriving:
        # being added to a group by a member is the invitation. Drop the
        # placeholder flag so they stop reading as one, and make sure the
        # account is active — a phantom that somehow carried a gated status
        # would otherwise lock out a legitimate new member.
        if user.get("isPhantom") or account_status(user) != STATUS_ACTIVE:
            await db.users.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {"status": STATUS_ACTIVE, "updatedAt": datetime.now(timezone.utc)},
                    "$unset": {"isPhantom": ""},
                },
            )
            user["status"] = STATUS_ACTIVE
            user.pop("isPhantom", None)
        return with_status(serialize_doc(user))

    # Create new user safely handling concurrent requests
    now = datetime.now(timezone.utc)
    
    # Check if this is the first user
    user_count = await db.users.count_documents({})
    is_first_user = user_count == 0
    role = "admin" if is_first_user else "user"
    invited_by = None
    invite = None

    status = STATUS_ACTIVE

    if not is_first_user:
        if not invite_code:
            # A code that was never supplied is recoverable — the account is
            # parked as PENDING and the app walks them through redeeming one.
            # A code that was supplied and is wrong is a typo worth reporting
            # straight away, so that case still fails below.
            if not allow_pending:
                raise HTTPException(status_code=400, detail="Invite code is required for registration.")
            status = STATUS_PENDING
        else:
            # An invite code is either a member's friendCode or an event join
            # code (the "claim your spot" group link), so a group link is enough
            # to sign up with — the group itself vouches for the new member.
            invite = await resolve_invite_code(db, invite_code)
            if not invite:
                raise HTTPException(status_code=400, detail="Invalid invite code.")
            invited_by = invite_code

    friend_code = generate_friend_code(8)

    password_hash = ""
    if password:
        password_hash = ph.hash(password)

    new_user_data = {
        "firebaseUid": uid,
        "email": email,
        "name": name or email.split("@")[0],
        "friendCode": friend_code,
        "upiId": "",
        "defaultCurrency": settings.DEFAULT_CURRENCY,
        "friends": [],
        "password": password_hash,
        "role": role,
        "invitedBy": invited_by,
        # The inviter's own id, so the tree survives them changing (or, on
        # deletion, losing) the friendCode that invitedBy records.
        "invitedByUserId": invite.get("inviterId") if invite else None,
        "status": status,
        "createdAt": now,
        "updatedAt": now,
    }
    
    import pymongo
    try:
        from pymongo import ReturnDocument
        # Using find_one_and_update with upsert=True prevents race conditions where
        # two concurrent requests both see no user and both try to insert.
        updated_user = await db.users.find_one_and_update(
            {"firebaseUid": uid},
            {"$setOnInsert": new_user_data},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        
        # Link phantom user expenses
        await link_phantom_expenses(db, email, str(updated_user["_id"]))
        await apply_invite(db, invite, updated_user["_id"])

        return with_status(serialize_doc(updated_user))
    except pymongo.errors.DuplicateKeyError:
        # Just in case there is a friendCode collision or another edge case, fallback to find
        user = await db.users.find_one({"firebaseUid": uid})
        if user:
            return with_status(serialize_doc(user))
        # If it still fails, it's a very rare friendCode collision, retry with a new code
        new_user_data["friendCode"] = generate_friend_code(8)
        updated_user = await db.users.find_one_and_update(
            {"firebaseUid": uid},
            {"$setOnInsert": new_user_data},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        
        # Link phantom user expenses
        await link_phantom_expenses(db, email, str(updated_user["_id"]))
        await apply_invite(db, invite, updated_user["_id"])

        return with_status(serialize_doc(updated_user))


async def resolve_invite_code(db: AsyncIOMotorDatabase, code: str) -> dict | None:
    """Resolve a signup invite code to its source.

    Two kinds of code are accepted:
    * a registered member's ``friendCode`` — the classic personal invite;
    * an event's ``joinCode`` — the group share link, so someone handed a group
      link can register and land straight in that group.

    Returns ``None`` when the code matches neither.
    """
    code = (code or "").strip().upper()
    if not code:
        return None

    # $nin also matches documents with no "status" at all, which is what
    # accounts predating the field look like — they count as active.
    inviter = await db.users.find_one(
        {
            "friendCode": code,
            "isDeleted": {"$ne": True},
            "status": {"$nin": [STATUS_INACTIVE, STATUS_PENDING]},
        },
        {"_id": 1, "name": 1},
    )
    if inviter:
        return {
            "type": "friend",
            "inviterId": inviter["_id"],
            "inviterName": inviter.get("name", ""),
        }

    event = await db.event.find_one(
        {"joinCode": code}, {"_id": 1, "eventName": 1, "createdBy": 1}
    )
    if event:
        return {
            "type": "event",
            "eventId": event["_id"],
            "eventName": event.get("eventName", ""),
            "inviterId": event.get("createdBy"),
        }
    return None


async def apply_invite(db: AsyncIOMotorDatabase, invite: dict | None, user_oid) -> None:
    """Act on the invite a new account was created with.

    A group join code also adds the new member to that event, so the link they
    followed actually lands them in the group instead of just unlocking signup.
    """
    if not invite or invite.get("type") != "event":
        return
    await db.event.update_one(
        {"_id": invite["eventId"]}, {"$addToSet": {"users": user_oid}}
    )


async def activate_with_invite(
    db: AsyncIOMotorDatabase, uid: str, invite_code: str
) -> dict:
    """Redeem an invite code for an account that is parked as PENDING.

    This is the only self-service way out of a gated account, and it is
    deliberately narrow: an account an admin switched off stays off, because
    borrowing a friend's code should not undo a moderation decision.
    """
    user = await db.users.find_one({"firebaseUid": uid})
    if not user or user.get("isDeleted"):
        raise HTTPException(status_code=404, detail="User not found")

    status = account_status(user)
    if status == STATUS_ACTIVE:
        return with_status(serialize_doc(user))
    if status == STATUS_INACTIVE:
        raise HTTPException(
            status_code=403,
            detail="This account has been deactivated. Contact an administrator.",
        )

    invite = await resolve_invite_code(db, invite_code)
    if not invite:
        raise HTTPException(status_code=400, detail="Invalid invite code.")
    if invite.get("inviterId") == user["_id"]:
        raise HTTPException(status_code=400, detail="You cannot use your own invite code.")

    now = datetime.now(timezone.utc)
    updated = await db.users.find_one_and_update(
        {"_id": user["_id"]},
        {
            "$set": {
                "status": STATUS_ACTIVE,
                "invitedBy": (invite_code or "").strip().upper(),
                "invitedByUserId": invite.get("inviterId"),
                "updatedAt": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    await apply_invite(db, invite, user["_id"])
    return with_status(serialize_doc(updated))


async def set_account_status(
    db: AsyncIOMotorDatabase, uid: str, status: str
) -> dict:
    """Switch an account on or off (admin action).

    Reactivating something that was never invited moves it out of PENDING for
    good — the admin turning it on is itself the vouching.
    """
    if status not in (STATUS_ACTIVE, STATUS_INACTIVE):
        raise HTTPException(status_code=400, detail="Status must be 'active' or 'inactive'")

    updated = await db.users.find_one_and_update(
        {"firebaseUid": uid, "isDeleted": {"$ne": True}},
        {"$set": {"status": status, "updatedAt": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="User not found")
    return with_status(serialize_doc(updated))


async def merge_phantom_into_user(
    db: AsyncIOMotorDatabase, phantom_id: str, user_id: str
) -> None:
    """Move everything a phantom placeholder holds onto a real account.

    Used when someone claims a spot in a group they were added to under a name
    the importer typed (rather than their own email, which the signup path
    already links by itself). Refuses anything that is not a phantom so a real
    account can never be absorbed into another.
    """
    phantom_oid = ObjectId(phantom_id)
    user_oid = ObjectId(user_id)
    if phantom_oid == user_oid:
        return

    phantom = await db.users.find_one({"_id": phantom_oid})
    if not phantom or not phantom.get("isPhantom"):
        raise HTTPException(status_code=400, detail="That member has already joined.")

    # Expense participation: shares, payShares and authorship.
    await db.expense.update_many(
        {"shares.userId": phantom_oid},
        {"$set": {"shares.$[elem].userId": user_oid}},
        array_filters=[{"elem.userId": phantom_oid}],
    )
    await db.expense.update_many(
        {"payShares.userId": phantom_oid},
        {"$set": {"payShares.$[elem].userId": user_oid}},
        array_filters=[{"elem.userId": phantom_oid}],
    )
    await db.expense.update_many(
        {"createdBy": phantom_oid}, {"$set": {"createdBy": user_oid}}
    )
    await db.expense.update_many(
        {"updatedBy": phantom_oid}, {"$set": {"updatedBy": user_oid}}
    )

    # Event membership: add the real user everywhere the phantom was, then drop
    # the phantom. Done in two steps so an event holding both never duplicates.
    await db.event.update_many(
        {"users": phantom_oid}, {"$addToSet": {"users": user_oid}}
    )
    await db.event.update_many({"users": phantom_oid}, {"$pull": {"users": phantom_oid}})
    await db.event.update_many({"createdBy": phantom_oid}, {"$set": {"createdBy": user_oid}})

    # Per-member FX overrides keyed by the phantom are meaningless now.
    await db.event.update_many(
        {f"memberRates.{phantom_id}": {"$exists": True}},
        {"$unset": {f"memberRates.{phantom_id}": ""}},
    )

    # Friendships: everyone who knew the phantom now knows the real user.
    await db.users.update_many(
        {"friends": phantom_oid}, {"$addToSet": {"friends": user_oid}}
    )
    await db.users.update_many({"friends": phantom_oid}, {"$pull": {"friends": phantom_oid}})
    await db.users.update_one({"_id": user_oid}, {"$pull": {"friends": phantom_oid}})

    await db.users.delete_one({"_id": phantom_oid})


async def create_phantom_user(
    db: AsyncIOMotorDatabase, email: str, name: str = ""
) -> dict:
    """Find a user by email, or create a placeholder ("phantom") account.

    Phantom users hold shares for people who haven't registered yet; when they
    sign up with the same email, link_phantom_expenses attaches their history.
    Unlike find_or_create_user this never requires an invite code.
    The returned dict has ``wasCreated`` so callers can send invites once.
    """
    import uuid as _uuid

    email = email.strip().lower()
    existing = await db.users.find_one({"email": email})
    if existing:
        result = serialize_doc(existing)
        result["wasCreated"] = False
        return result

    friend_code = generate_friend_code(8)
    while await db.users.find_one({"friendCode": friend_code}):
        friend_code = generate_friend_code(8)

    now = datetime.now(timezone.utc)
    doc = {
        "firebaseUid": f"phantom_{_uuid.uuid4().hex}",
        "email": email,
        "name": name or email.split("@")[0],
        "friendCode": friend_code,
        "upiId": "",
        "defaultCurrency": settings.DEFAULT_CURRENCY,
        "friends": [],
        "password": "",
        "role": "user",
        "status": STATUS_ACTIVE,
        "isPhantom": True,
        "invitedBy": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    serialized = serialize_doc(doc)
    serialized["wasCreated"] = True
    return serialized


async def link_phantom_expenses(db: AsyncIOMotorDatabase, email: str, user_id_str: str):
    """Link expenses where the user was invited by email before registration."""
    # We update shares where email matches, setting the userId.
    # NB: the expense collection is `db.expense` (singular) — this previously
    # targeted a nonexistent `db.expenses`, silently linking nothing.
    await db.expense.update_many(
        {"shares.email": email},
        {"$set": {"shares.$[elem].userId": user_id_str}},
        array_filters=[{"elem.email": email}]
    )
    await db.expense.update_many(
        {"payShares.email": email},
        {"$set": {"payShares.$[elem].userId": user_id_str}},
        array_filters=[{"elem.email": email}]
    )


async def get_user_by_firebase_uid(db: AsyncIOMotorDatabase, uid: str) -> dict | None:
    """Get user by Firebase UID."""
    user = await db.users.find_one({"firebaseUid": uid})
    return serialize_doc(user) if user else None


async def get_user_by_id(db: AsyncIOMotorDatabase, user_id: str) -> dict | None:
    """Get user by MongoDB ObjectId string."""
    try:
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        return serialize_doc(user) if user else None
    except Exception:
        return None


async def get_user_name_by_id(db: AsyncIOMotorDatabase, user_id: str) -> str:
    """Get just the user's name by ID."""
    user = await get_user_by_id(db, user_id)
    return user["name"] if user else "Unknown"


async def get_user_account(db: AsyncIOMotorDatabase, uid: str) -> dict | None:
    """Get full account info for the current user."""
    user = await db.users.find_one({"firebaseUid": uid})
    if not user:
        return None
        
    friend_code = user.get("friendCode", "")
    if not friend_code:
        # Auto-generate if missing
        friend_code = generate_friend_code(8)
        while await db.users.find_one({"friendCode": friend_code}):
            friend_code = generate_friend_code(8)
        await db.users.update_one(
            {"_id": user["_id"]}, {"$set": {"friendCode": friend_code}}
        )

    return {
        "id": str(user["_id"]),
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "friendCode": friend_code,
        "upiId": user.get("upiId", ""),
        "defaultCurrency": user.get("defaultCurrency", settings.DEFAULT_CURRENCY),
        "uuid": friend_code,
        "role": user.get("role", "user"),
        "invitedBy": user.get("invitedBy"),
        "status": account_status(user),
    }


async def update_user_account(
    db: AsyncIOMotorDatabase, uid: str, data: dict
) -> bool:
    """Update user account fields (name, upiId, defaultCurrency)."""
    from app.services import fx_service

    update_fields = {"updatedAt": datetime.now(timezone.utc)}
    if "name" in data and data["name"]:
        update_fields["name"] = data["name"]
    if "upiId" in data and data["upiId"] is not None:
        update_fields["upiId"] = data["upiId"]
    if data.get("defaultCurrency"):
        currency = data["defaultCurrency"].strip().upper()
        if currency not in fx_service.SUPPORTED_CURRENCIES:
            raise HTTPException(status_code=400, detail="Unsupported currency")
        update_fields["defaultCurrency"] = currency

    result = await db.users.update_one(
        {"firebaseUid": uid}, {"$set": update_fields}
    )
    return result.modified_count > 0 or result.matched_count > 0


async def change_password(
    db: AsyncIOMotorDatabase, uid: str, password: str
) -> dict:
    """Update stored password hash."""
    password_hash = ph.hash(password)
    await db.users.update_one(
        {"firebaseUid": uid},
        {"$set": {"password": password_hash, "updatedAt": datetime.now(timezone.utc)}},
    )
    return {"message": "Password updated successfully"}


async def get_user_internal_id(db: AsyncIOMotorDatabase, uid: str) -> str | None:
    """Get the MongoDB ObjectId string for a Firebase UID user."""
    user = await db.users.find_one({"firebaseUid": uid}, {"_id": 1})
    return str(user["_id"]) if user else None


async def get_invited_users(
    db: AsyncIOMotorDatabase, friend_code: str, user_id=None
) -> list[dict]:
    """Everyone this member brought in.

    Matches on the inviter's id as well as their code, so the list survives a
    friendCode changing. Accounts that have since been deleted are left out —
    they are anonymous tombstones, and listing "Deleted User" as someone you
    referred is noise.
    """
    match = [{"invitedBy": friend_code}] if friend_code else []
    if user_id is not None:
        match.append({"invitedByUserId": ObjectId(str(user_id))})
    if not match:
        return []

    cursor = db.users.find(
        {"$or": match, "isDeleted": {"$ne": True}}
    ).sort("createdAt", -1)
    users = await cursor.to_list(length=100)
    return [serialize_doc(u) for u in users]


async def build_invite_tree(db: AsyncIOMotorDatabase) -> list[dict]:
    """The whole instance as a who-invited-whom forest (admin view).

    Parentage prefers ``invitedByUserId`` and falls back to re-resolving the
    stored ``invitedBy`` code, because accounts created before that field
    existed only kept the code. A code resolves to its owner, or — for a group
    join link — to whoever created that group.

    Deleted accounts are dropped and everyone below them re-parented to
    whoever invited *them*, so a tombstone mid-chain does not orphan a whole
    branch. Phantom placeholders only appear if someone hangs off them;
    on their own they are group bookkeeping, not people who signed up.
    """
    projection = {
        "name": 1, "email": 1, "friendCode": 1, "role": 1, "status": 1,
        "isDeleted": 1, "isPhantom": 1, "invitedBy": 1, "invitedByUserId": 1,
        "createdAt": 1,
    }
    users = await db.users.find({}, projection).to_list(length=None)
    by_id = {u["_id"]: u for u in users}
    by_code = {u["friendCode"]: u["_id"] for u in users if u.get("friendCode")}

    join_codes = {}
    async for event in db.event.find(
        {"joinCode": {"$exists": True}}, {"joinCode": 1, "createdBy": 1}
    ):
        if event.get("joinCode") and event.get("createdBy"):
            join_codes[event["joinCode"]] = event["createdBy"]

    def stated_parent(uid, user):
        parent = user.get("invitedByUserId")
        if parent not in by_id:
            code = (user.get("invitedBy") or "").strip().upper()
            parent = (by_code.get(code) or join_codes.get(code)) if code else None
        # Self-invites cannot happen through the API, but a hand-edited
        # document must not become its own ancestor.
        return parent if parent in by_id and parent != uid else None

    parents = {uid: stated_parent(uid, u) for uid, u in by_id.items()}

    def living_parent(uid):
        """First ancestor that is not a tombstone."""
        seen = {uid}
        parent = parents.get(uid)
        while parent is not None and by_id[parent].get("isDeleted"):
            if parent in seen:
                return None
            seen.add(parent)
            parent = parents.get(parent)
        return parent

    children: dict = {}
    roots: list = []
    for uid, user in by_id.items():
        if user.get("isDeleted"):
            continue
        parent = living_parent(uid)
        if parent is None:
            roots.append(uid)
        else:
            children.setdefault(parent, []).append(uid)

    def sort_key(uid):
        return (by_id[uid].get("name") or "").lower()

    def build(uid, ancestors):
        user = by_id[uid]
        kids = []
        for child in sorted(children.get(uid, []), key=sort_key):
            if child in ancestors:
                continue
            built = build(child, ancestors | {uid})
            if built is not None:
                kids.append(built)
        if user.get("isPhantom") and not kids:
            return None
        return {
            "id": str(uid),
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "friendCode": user.get("friendCode", ""),
            "role": user.get("role", "user"),
            "status": account_status(user),
            "isPhantom": bool(user.get("isPhantom")),
            "createdAt": _serialize_value(user.get("createdAt")),
            "inviteCount": len(kids),
            # Everyone underneath, however deep — the number an admin actually
            # wants when asking how far one person's referrals reached.
            "totalInvited": len(kids) + sum(k["totalInvited"] for k in kids),
            "children": kids,
        }

    tree = [build(uid, frozenset()) for uid in sorted(roots, key=sort_key)]
    return [node for node in tree if node is not None]


async def _delete_supabase_auth_user(uid: str) -> None:
    """Best-effort removal of the Supabase auth record.

    Needs SUPABASE_SERVICE_ROLE_KEY; without it (or on error) the Mongo-side
    deletion still stands — the orphaned auth record can no longer reach any
    data because the tombstoned user fails every lookup.
    """
    import httpx

    key = settings.SUPABASE_SERVICE_ROLE_KEY
    if not key or uid.startswith(("phantom_", "deleted_")):
        return
    url = f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/admin/users/{uid}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                url, headers={"apikey": key, "Authorization": f"Bearer {key}"}
            )
            if resp.status_code not in (200, 204, 404):
                print(
                    "WARNING: Supabase auth deletion returned "
                    f"{resp.status_code} for {uid}"
                )
    except Exception as e:
        print(f"WARNING: Supabase auth deletion failed for {uid}: {e}")


async def delete_user_account(db: AsyncIOMotorDatabase, uid: str) -> bool:
    """Permanently delete the user's account and personal data.

    Store-required deletion flow (App Store 5.1.1(v) / Play policy). Data
    owned solely by the user is removed; records shared with other people are
    kept for their balances' integrity but point at an anonymous tombstone
    ("Deleted User") with every personal field erased.
    """
    import uuid as _uuid

    user = await db.users.find_one({"firebaseUid": uid})
    if not user:
        return False
    oid = user["_id"]

    # Solo data: personal expenses and public settle-up pages.
    await db.expense.delete_many({"type": "normal", "createdBy": oid})
    await db.payment_page.delete_many({"userId": oid})

    # Drop the user from everyone's friends list.
    await db.users.update_many({"friends": oid}, {"$pull": {"friends": oid}})

    # Anonymous tombstone: shared expenses keep resolving to "Deleted User".
    # friendCode is unset so it can never be used as an invite again (the
    # unique index is sparse), and the random firebaseUid can never collide
    # with a real auth subject.
    now = datetime.now(timezone.utc)
    await db.users.update_one(
        {"_id": oid},
        {
            "$set": {
                "firebaseUid": f"deleted_{_uuid.uuid4().hex}",
                "email": "",
                "name": "Deleted User",
                "upiId": "",
                "password": "",
                "friends": [],
                "role": "user",
                "status": STATUS_INACTIVE,
                "isDeleted": True,
                "deletedAt": now,
                "updatedAt": now,
            },
            "$unset": {"friendCode": ""},
        },
    )

    await _delete_supabase_auth_user(uid)
    return True
