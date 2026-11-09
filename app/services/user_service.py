import re
import secrets
import string
from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import HTTPException

from app.config import settings

#: Two-letter country code (ISO 3166-1 alpha-2, e.g. "IN").
_COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
#: Pragmatic email shape check — the same one used by the settlement
#: service. PayPal rejects a malformed payee address with a clear API
#: error anyway; this catches typos at save time.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def generate_friend_code(length: int = 8) -> str:
    """Generate a unique alphanumeric friend code.

    Uses `secrets`, not `random`: this code is both the registration invite
    code and the add-friend key, matching payment_service.generate_link.
    """
    num_digits = 2
    num_letters = length - num_digits
    digits = "".join(secrets.choice(string.digits) for _ in range(num_digits))
    letters = "".join(
        secrets.choice(string.ascii_uppercase) for _ in range(num_letters)
    )
    code_list = list(digits + letters)
    # Fisher-Yates over secrets.randbelow; random.shuffle would reintroduce
    # the Mersenne Twister this function just avoided.
    for i in range(len(code_list) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        code_list[i], code_list[j] = code_list[j], code_list[i]
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


# ─── Account status ─────────────────────────────────────────────────────
# ACTIVE accounts work normally. INACTIVE ones were switched off by an admin
# and can do nothing until an admin switches them back on. PENDING ones signed
# in but never supplied an invite code: they exist so an admin can see them,
# and they unlock themselves by redeeming a code (see activate_with_invite).
#
# Accounts written before this field existed have no "status" at all, which
# reads as ACTIVE — see account_status(). Placeholder members are deliberately
# left ACTIVE too: nobody signs in *as* a placeholder, but the real person does
# sign up on that same document (find_or_create_user claims it by email), and a
# gated status there would lock out someone holding a valid invite code.
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_PENDING = "pending"
ACCOUNT_STATUSES = (STATUS_ACTIVE, STATUS_INACTIVE, STATUS_PENDING)

#: Statuses that cannot vouch for anyone. Used with $nin, which also matches
#: documents having no "status" field — exactly how pre-existing accounts look.
GATED_STATUSES = [STATUS_INACTIVE, STATUS_PENDING]


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


#: Never leaves the service layer. ``password`` is a legacy argon2 hash of the
#: user's real Supabase credential; it is no longer written (see
#: find_or_create_user) but still exists on documents created before that
#: change, so strip it defensively rather than trusting the migration.
_PRIVATE_USER_FIELDS = frozenset({"password"})

#: The only user fields safe to hand to *another* user. Anything describing a
#: capability (friendCode, which is both the invite code and the add-friend
#: key) or another user's relationships stays out.
_PUBLIC_USER_FIELDS = frozenset({"id", "name", "email"})


def serialize_doc(doc: dict) -> dict:
    """Convert MongoDB document to JSON-serializable dict (recursively).

    Strips credential fields so that a user document can never leak them, even
    when a caller forgets to project. Use :func:`public_user` when returning
    someone *other* than the caller.
    """
    if doc is None:
        return None
    result = {}
    for key, value in doc.items():
        if key in _PRIVATE_USER_FIELDS:
            continue
        if key == "_id":
            result["id"] = str(value)
        else:
            result[key] = _serialize_value(value)
    return result


def public_user(doc: dict) -> dict:
    """Serialize a user document for disclosure to a *different* user."""
    if doc is None:
        return None
    serialized = serialize_doc(doc)
    return {k: v for k, v in serialized.items() if k in _PUBLIC_USER_FIELDS}


async def find_or_create_user(
    db: AsyncIOMotorDatabase, uid: str, email: str, name: str = "", invite_code: str = "",
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

    if not email:
        raise HTTPException(
            status_code=400, detail="Authenticated token carries no email address."
        )

    # Claim an existing account by email — legacy records and phantom users
    # created before this person signed up. Restricted to accounts that are not
    # already bound to a real Supabase identity: without that guard, anyone who
    # could present a token for `email` would take over the live account.
    user = await db.users.find_one(
        {
            "email": email,
            "$or": [
                {"firebaseUid": {"$exists": False}},
                {"firebaseUid": None},
                {"firebaseUid": ""},
                {"firebaseUid": {"$regex": "^phantom_"}},
                {"isPhantom": True},
            ],
        }
    )
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

        # Signing in on a placeholder's document *is* that person arriving:
        # being added to a group by a member is the invitation. Drop the
        # placeholder flags so they stop reading as one, and make sure the
        # account is active — a placeholder that somehow carried a gated
        # status would otherwise lock out a legitimate new member.
        if user.get("isPhantom") or account_status(user) != STATUS_ACTIVE:
            await db.users.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {"status": STATUS_ACTIVE, "updatedAt": datetime.now(timezone.utc)},
                    "$unset": {"isPhantom": "", "claimable": ""},
                },
            )
            user["status"] = STATUS_ACTIVE
            user.pop("isPhantom", None)
            user.pop("claimable", None)
        return with_status(serialize_doc(user))

    # Create new user safely handling concurrent requests
    now = datetime.now(timezone.utc)
    
    # Check if this is the first user
    user_count = await db.users.count_documents({})
    is_first_user = user_count == 0
    role = "admin" if is_first_user else "user"
    invited_by = None
    invited_by_user_id = None
    status = STATUS_ACTIVE

    # ── Signup gating (admin-controlled) ────────────────────────────────
    # The first user (admin bootstrap) always passes. After that, the
    # admin can disable signups or cap the number of registrations.
    if not is_first_user:
        from app.services.site_settings import get_site_settings
        ss = await get_site_settings(db)
        if not ss.get("signupsEnabled", True):
            raise HTTPException(
                status_code=403,
                detail="New registrations are currently disabled by the administrator.",
            )
        max_limit = ss.get("maxSignupLimit", 0)
        if max_limit > 0:
            real_count = await db.users.count_documents(
                {"isPhantom": {"$ne": True}, "isDeleted": {"$ne": True}}
            )
            if real_count >= max_limit:
                raise HTTPException(
                    status_code=403,
                    detail="The maximum number of registered users has been reached. New sign-ups are temporarily disabled.",
                )

    if not is_first_user and not invite_code:
        # A code that was never supplied is recoverable — the account is parked
        # as PENDING and the app walks them through redeeming one. A code that
        # was supplied and is wrong is a typo worth reporting straight away, so
        # that case still fails below.
        if not allow_pending:
            raise HTTPException(status_code=400, detail="Invite code is required for registration.")
        status = STATUS_PENDING
    elif not is_first_user:
        # Validate invite code. Two kinds are accepted: another user's
        # friendCode, and a group's join code — someone handed a group link is
        # just as invited as someone handed a personal code, and without this
        # they would hit "invite code required" with no way to get one.
        #
        # Group links normally carry the sharer's personal code too, so this
        # fallback is for links that lost their query string. Either way
        # `invitedBy` ends up holding a *person's* code: attribution falls back
        # to whoever created the group, so the referral chain never breaks.
        # A gated account cannot vouch for anyone: $nin also matches documents
        # with no "status", which is what pre-existing accounts look like.
        inviter = await db.users.find_one(
            {"friendCode": invite_code, "status": {"$nin": GATED_STATUSES}}
        )
        if inviter:
            invited_by = invite_code
            invited_by_user_id = inviter["_id"]
        else:
            group = await db.event.find_one(
                {"joinCode": invite_code.strip().upper()}, {"createdBy": 1}
            )
            if not group:
                raise HTTPException(status_code=400, detail="Invalid invite code.")
            creator = await db.users.find_one(
                {"_id": group.get("createdBy")}, {"friendCode": 1}
            )
            invited_by = (creator or {}).get("friendCode") or invite_code
            invited_by_user_id = group.get("createdBy")

    friend_code = generate_friend_code(8)

    # No password is stored. Supabase is the sole authenticator — nothing in
    # this backend ever verified the hash we used to keep here, so it was a
    # shadow copy of the user's real credential with no function.
    new_user_data = {
        "firebaseUid": uid,
        "email": email,
        "name": name or email.split("@")[0],
        "friendCode": friend_code,
        "upiId": "",
        # Settlement profile fields — empty until the user fills them in.
        # country: ISO alpha-2 ("IN", "US", ...); paypalEmail: the PayPal
        # account that receives settlements. See the settlement service.
        "country": "",
        "paypalEmail": "",
        "defaultCurrency": settings.DEFAULT_CURRENCY,
        "friends": [],
        "role": role,
        "invitedBy": invited_by,
        # The inviter's own id, so the tree survives them changing (or, on
        # deletion, losing) the friendCode that invitedBy records.
        "invitedByUserId": invited_by_user_id,
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

        return with_status(serialize_doc(updated_user))


async def activate_with_invite(
    db: AsyncIOMotorDatabase, uid: str, invite_code: str
) -> dict:
    """Redeem an invite code for an account that is parked as PENDING.

    This is the only self-service way out of a gated account, and it is
    deliberately narrow: an account an admin switched off stays off, because
    borrowing a friend's code should not undo a moderation decision.
    """
    from pymongo import ReturnDocument

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

    code = (invite_code or "").strip()
    # Same two kinds of code, and the same normalisation, as signup: whatever
    # is stored in invitedBy must be a *person's* friendCode so the referral
    # chain never dead-ends on a group.
    inviter = await db.users.find_one(
        {"friendCode": code, "status": {"$nin": GATED_STATUSES}}
    )
    if inviter:
        invited_by, inviter_id = code, inviter["_id"]
    else:
        group = await db.event.find_one({"joinCode": code.upper()}, {"createdBy": 1})
        if not group:
            raise HTTPException(status_code=400, detail="Invalid invite code.")
        creator = await db.users.find_one(
            {"_id": group.get("createdBy")}, {"friendCode": 1}
        )
        invited_by = (creator or {}).get("friendCode") or code
        inviter_id = group.get("createdBy")

    if inviter_id == user["_id"]:
        raise HTTPException(status_code=400, detail="You cannot use your own invite code.")

    updated = await db.users.find_one_and_update(
        {"_id": user["_id"]},
        {
            "$set": {
                "status": STATUS_ACTIVE,
                "invitedBy": invited_by,
                "invitedByUserId": inviter_id,
                "updatedAt": datetime.now(timezone.utc),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return with_status(serialize_doc(updated))


async def set_account_status(
    db: AsyncIOMotorDatabase, uid: str, status: str
) -> dict:
    """Switch an account on or off (admin action).

    Reactivating something that was never invited moves it out of PENDING for
    good — the admin turning it on is itself the vouching.
    """
    from pymongo import ReturnDocument

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
        # This is a lookup of someone else's account by email, so return only
        # what the caller needs to attach a share. Returning the raw document
        # here handed any authenticated user an arbitrary victim's friendCode
        # (the invite/add-friend key) and legacy password hash.
        result = public_user(existing)
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
        "role": "user",
        "status": STATUS_ACTIVE,
        "isPhantom": True,
        "invitedBy": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    # Same shape as the existing-user branch above; callers only need the id
    # and enough to address an invite email.
    serialized = public_user(doc)
    serialized["wasCreated"] = True
    return serialized


async def create_placeholder_user(
    db: AsyncIOMotorDatabase, name: str, created_by: str | None = None
) -> dict:
    """Create a name-only placeholder member — someone with no email yet.

    The difference from :func:`create_phantom_user` is that there is no address
    to match on later, so this always creates a fresh document instead of
    finding one: two people called "Sam" in two different groups are two
    different placeholders. They are claimed explicitly instead, by whoever
    joins the group and says "that one is me" (event_service.join_event_by_code).

    ``claimable`` marks that: an email-backed phantom belongs to that address
    and must never be claimable by an arbitrary link holder.
    """
    import uuid as _uuid

    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Member name is required")

    friend_code = generate_friend_code(8)
    while await db.users.find_one({"friendCode": friend_code}):
        friend_code = generate_friend_code(8)

    now = datetime.now(timezone.utc)
    doc = {
        "firebaseUid": f"phantom_{_uuid.uuid4().hex}",
        # Empty, not absent: the email-claim branch of find_or_create_user
        # searches by a real address, which never matches "".
        "email": "",
        "name": name,
        "friendCode": friend_code,
        "upiId": "",
        "defaultCurrency": settings.DEFAULT_CURRENCY,
        "friends": [],
        "role": "user",
        "status": STATUS_ACTIVE,
        "isPhantom": True,
        "claimable": True,
        "createdBy": ObjectId(created_by) if created_by else None,
        "invitedBy": None,
        "createdAt": now,
        "updatedAt": now,
    }
    result = await db.users.insert_one(doc)
    doc["_id"] = result.inserted_id
    serialized = public_user(doc)
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
        "country": user.get("country", ""),
        "paypalEmail": user.get("paypalEmail", ""),
        "defaultCurrency": user.get("defaultCurrency", settings.DEFAULT_CURRENCY),
        "fxRates": user.get("fxRates", {}),
        "uuid": friend_code,
        "role": user.get("role", "user"),
        "invitedBy": user.get("invitedBy"),
        "status": account_status(user),
    }


async def update_user_account(
    db: AsyncIOMotorDatabase, uid: str, data: dict
) -> bool:
    """Update user account fields (name, upiId, defaultCurrency, country,
    paypalEmail, fxRates)."""
    from app.services import fx_service

    update_fields = {"updatedAt": datetime.now(timezone.utc)}
    if "name" in data and data["name"]:
        update_fields["name"] = data["name"]
    if "upiId" in data and data["upiId"] is not None:
        update_fields["upiId"] = data["upiId"]
    if data.get("country") is not None:
        country = str(data["country"]).strip().upper()
        if country and not _COUNTRY_RE.match(country):
            raise HTTPException(
                status_code=400,
                detail="country must be a 2-letter ISO country code (e.g. IN, US)",
            )
        update_fields["country"] = country
    if data.get("paypalEmail") is not None:
        email = str(data["paypalEmail"]).strip()
        if email and not _EMAIL_RE.match(email):
            raise HTTPException(
                status_code=400, detail="paypalEmail is not a valid email address"
            )
        update_fields["paypalEmail"] = email
    if data.get("defaultCurrency"):
        currency = data["defaultCurrency"].strip().upper()
        if currency not in fx_service.SUPPORTED_CURRENCIES:
            raise HTTPException(status_code=400, detail="Unsupported currency")
        update_fields["defaultCurrency"] = currency
    if data.get("fxRates") is not None:
        update_fields["fxRates"] = fx_service.clean_rate_table(data["fxRates"])

    result = await db.users.update_one(
        {"firebaseUid": uid}, {"$set": update_fields}
    )
    return result.modified_count > 0 or result.matched_count > 0


async def change_password(
    db: AsyncIOMotorDatabase, uid: str, password: str = "") -> dict:
    """Acknowledge a password change.

    The password itself lives only in Supabase, which the client updates
    directly; this backend stores no copy. Kept as an endpoint so existing
    clients keep working, and it clears any legacy hash left on the document.
    """
    await db.users.update_one(
        {"firebaseUid": uid},
        {
            "$set": {"updatedAt": datetime.now(timezone.utc)},
            "$unset": {"password": ""},
        },
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
    referred is noise. The projection stays narrow: this is other people's
    data, so it carries no capability fields (see _PUBLIC_USER_FIELDS).
    """
    match = [{"invitedBy": friend_code}] if friend_code else []
    if user_id is not None:
        match.append({"invitedByUserId": ObjectId(str(user_id))})
    if not match:
        return []

    cursor = db.users.find(
        {"$or": match, "isDeleted": {"$ne": True}},
        {"_id": 1, "name": 1, "email": 1, "createdAt": 1},
    ).sort("createdAt", -1)
    users = await cursor.to_list(length=100)
    return [serialize_doc(u) for u in users]


async def build_invite_tree(db: AsyncIOMotorDatabase) -> list[dict]:
    """The whole instance as a who-invited-whom forest (admin view).

    Parentage prefers ``invitedByUserId`` and falls back to re-resolving the
    stored ``invitedBy`` code, because accounts created before that field
    existed only kept the code. Signup normalises that code to a *person's*
    friendCode even when a group link was used, so resolving it by code is
    enough; group join codes are resolved too, for anything written before
    that normalisation.

    Deleted accounts are dropped and everyone below them re-parented to
    whoever invited *them*, so a tombstone mid-chain does not orphan a whole
    branch. Placeholder members only appear if someone hangs off them; on
    their own they are group bookkeeping, not people who signed up.
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
                "friends": [],
                "role": "user",
                "status": STATUS_INACTIVE,
                "isDeleted": True,
                "deletedAt": now,
                "updatedAt": now,
            },
            "$unset": {"friendCode": "", "password": ""},
        },
    )

    await _delete_supabase_auth_user(uid)
    return True
