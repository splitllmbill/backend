import os
import pytest
from fastapi.testclient import TestClient

# Override config to use a test database BEFORE importing app modules
os.environ["DB_NAME"] = "test_splitllm"

from app.main import app
from app.database import connect_to_mongo, close_mongo_connection, get_db
from app.auth import get_current_user

# Global mock user ID to be used across tests
MOCK_USER_UID = "test_user_uid_123"
MOCK_USER_EMAIL = "test@example.com"
MOCK_USER_NAME = "Test User"

async def mock_get_current_user():
    return {
        "uid": MOCK_USER_UID,
        "email": MOCK_USER_EMAIL,
        "name": MOCK_USER_NAME,
    }

# Mock out the authentication dependency
app.dependency_overrides[get_current_user] = mock_get_current_user

@pytest.fixture(scope="session", autouse=True)
async def setup_database():
    """Connect to test database before running tests and close after."""
    await connect_to_mongo()
    db = get_db()
    
    # Clean up test database before starting
    await db.users.delete_many({})
    await db.event.delete_many({})
    await db.expense.delete_many({})
    await db.category_hints.delete_many({})
    
    # Pre-seed a test user
    from app.services.user_service import find_or_create_user
    await find_or_create_user(db, uid=MOCK_USER_UID, email=MOCK_USER_EMAIL, name=MOCK_USER_NAME)
    
    yield
    
    # Clean up after tests
    await db.users.delete_many({})
    await db.event.delete_many({})
    await db.expense.delete_many({})
    await db.category_hints.delete_many({})
    
    await close_mongo_connection()

@pytest.fixture
def client():
    """Returns a TestClient instance."""
    with TestClient(app) as client:
        yield client
