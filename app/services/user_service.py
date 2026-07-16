import random
import string
from datetime import datetime, timezone
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase
from argon2 import PasswordHasher
from fastapi import HTTPException

from app.config import settings

ph = PasswordHasher()


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
    db: AsyncIOMotorDatabase, uid: str, email: str, name: str = "", password: str = "", invite_code: str = ""
) -> dict:
    """Find user by Firebase UID, or create one if not exists."""
    user = await db.users.find_one({"firebaseUid": uid})
    if user:
        return serialize_doc(user)

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
        return serialize_doc(user)

    # Create new user safely handling concurrent requests
    now = datetime.now(timezone.utc)
    
    # Check if this is the first user
    user_count = await db.users.count_documents({})
    is_first_user = user_count == 0
    role = "admin" if is_first_user else "user"
    invited_by = None
    
    if not is_first_user:
        if not invite_code:
            raise HTTPException(status_code=400, detail="Invite code is required for registration.")
        # Validate invite code (friendCode)
        inviter = await db.users.find_one({"friendCode": invite_code})
        if not inviter:
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
        
        return serialize_doc(updated_user)
    except pymongo.errors.DuplicateKeyError:
        # Just in case there is a friendCode collision or another edge case, fallback to find
        user = await db.users.find_one({"firebaseUid": uid})
        if user:
            return serialize_doc(user)
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
        
        return serialize_doc(updated_user)


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


async def get_invited_users(db: AsyncIOMotorDatabase, friend_code: str) -> list[dict]:
    """Get all users invited by this friend code."""
    cursor = db.users.find({"invitedBy": friend_code})
    users = await cursor.to_list(length=100)
    return [serialize_doc(u) for u in users]


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
                "isDeleted": True,
                "deletedAt": now,
                "updatedAt": now,
            },
            "$unset": {"friendCode": ""},
        },
    )

    await _delete_supabase_auth_user(uid)
    return True
