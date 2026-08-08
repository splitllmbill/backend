import asyncio

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.config import settings

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def _seed_database(db: AsyncIOMotorDatabase):
    """Seed initial data like LLM prompts if they don't exist."""
    prompt_count = await db.prompts.count_documents({"type": "chat"})
    if prompt_count == 0:
        await db.prompts.insert_one({
            "type": "chat",
            "version": 1,
            "prompt": "Extract expense information from natural language. Output ONLY a valid JSON array of objects with: 'expenseName' (string), 'amount' (number). Include 'splitBetween' (array of names) and 'paidBy' (name) if specified. Example: [{\"expenseName\": \"dinner\", \"amount\": 100, \"paidBy\": \"Alice\", \"splitBetween\": [\"Alice\", \"Bob\"]}]\n\nText: ",
            "hits": 0,
            "successHits": 0,
            "failureHits": 0
        })
        print("Seeded 'chat' prompt in database")

    import pymongo
    await db.users.create_index([("firebaseUid", pymongo.ASCENDING)], unique=True, sparse=True)
    await db.users.create_index([("friendCode", pymongo.ASCENDING)], unique=True, sparse=True)
    # Group join links are looked up by code, and must be unique.
    await db.event.create_index(
        [("joinCode", pymongo.ASCENDING)], unique=True, sparse=True
    )
    # One tally per (owner, token); the global tally has userId: null.
    await db.category_hints.create_index(
        [("userId", pymongo.ASCENDING), ("token", pymongo.ASCENDING)], unique=True
    )

    # Group invite links resolve by code on every visit, and two groups sharing
    # a code would let an invitee land in the wrong one.
    await db.event.create_index(
        [("joinCode", pymongo.ASCENDING)], unique=True, sparse=True
    )

    # Activity feed: every read is "entries visible to me, newest first".
    await db.audit_log.create_index(
        [("visibleTo", pymongo.ASCENDING), ("createdAt", pymongo.DESCENDING)]
    )
    # Settle-ups are looked up by either party, and only pending ones matter.
    await db.settlement_request.create_index(
        [("payerId", pymongo.ASCENDING), ("status", pymongo.ASCENDING)]
    )
    await db.settlement_request.create_index(
        [("payeeId", pymongo.ASCENDING), ("status", pymongo.ASCENDING)]
    )
    await db.credit_cards.create_index([("userId", pymongo.ASCENDING)])
    # This unique index *is* the reminder de-duplication: a second attempt to
    # send the same (card, due date, lead time) fails the insert instead of
    # mailing the user twice. See card_service._claim_reminder.
    await db.card_reminder_log.create_index(
        [
            ("cardId", pymongo.ASCENDING),
            ("dueDate", pymongo.ASCENDING),
            ("leadDays", pymongo.ASCENDING),
        ],
        unique=True,
    )

    # Drop the legacy `password` field. Supabase is the sole authenticator and
    # nothing here ever verified this hash, so it was a shadow copy of the
    # user's real credential. Idempotent: a no-op once every document is clean.
    purged = await db.users.update_many(
        {"password": {"$exists": True}}, {"$unset": {"password": ""}}
    )
    if purged.modified_count:
        print(f"Purged legacy password hash from {purged.modified_count} user(s)")

async def connect_to_mongo():
    """Initialize MongoDB connection on app startup.

    A failed ping logs a warning instead of crashing the process, so the app
    still boots (and /health responds) when the database is unreachable or not
    yet configured — DB-backed endpoints will error per-request until it is.
    """
    global _client, _db
    _client = AsyncIOMotorClient(
        settings.MONGODB_URI, serverSelectionTimeoutMS=5000
    )
    _db = _client[settings.DB_NAME]
    try:
        # Verify connection
        await _client.admin.command("ping")
        print(f"Connected to MongoDB: {settings.DB_NAME}")

        # Run seeders
        await _seed_database(_db)
    except Exception as e:
        print(
            f"WARNING: MongoDB not reachable at startup ({e}); "
            "continuing without a verified connection"
        )


async def ping_database(timeout_s: float = 5.0) -> tuple[bool, str | None]:
    """Check whether Mongo is currently reachable.

    Returns (ok, reason). Never raises — a dead database is a reportable state,
    not a crash.

    `timeout_s` caps how long the caller blocks. It matters: the driver's own
    serverSelectionTimeoutMS is 5s, and a /health that blocks that long would
    exceed the liveness probe's timeout and get the container killed for a
    failure a restart cannot fix. Liveness callers pass a short timeout.

    The reason is a short, non-sensitive summary — the full driver error names
    the Mongo host and port, and these endpoints are public and unauthenticated,
    so the detail goes to the log instead.
    """
    if _client is None:
        return False, "not initialized"
    try:
        await asyncio.wait_for(_client.admin.command("ping"), timeout=timeout_s)
        return True, None
    except asyncio.TimeoutError:
        print(f"WARNING: database ping exceeded {timeout_s}s")
        return False, "timeout"
    except Exception as e:
        print(f"WARNING: database ping failed: {e}")
        return False, type(e).__name__


async def close_mongo_connection():
    """Close MongoDB connection on app shutdown."""
    global _client
    if _client:
        _client.close()
        print("MongoDB connection closed")


def get_db() -> AsyncIOMotorDatabase:
    """Get the database instance. Used as a FastAPI dependency."""
    if _db is None:
        raise RuntimeError("Database not initialized. Call connect_to_mongo() first.")
    return _db
