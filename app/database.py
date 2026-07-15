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
