import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.auth import get_admin_user, get_current_user

# Mock admin user
async def mock_get_admin_user():
    return {
        "uid": "admin_uid_123",
        "email": "admin@example.com",
        "name": "Admin User",
        "role": "admin"
    }

@pytest.fixture
def admin_client():
    """Client with admin user overridden."""
    saved_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_admin_user] = mock_get_admin_user
    # Also override current_user to be admin for consistency if needed
    app.dependency_overrides[get_current_user] = mock_get_admin_user
    with TestClient(app) as client:
        yield client
    # Restore (don't clear: conftest registers a session-wide auth override)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved_overrides)

def test_list_users(admin_client):
    response = admin_client.get("/db/admin/users")
    assert response.status_code == 200
    users = response.json()
    assert isinstance(users, list)

def test_list_issues(admin_client):
    response = admin_client.get("/db/admin/issues")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_get_models(admin_client):
    response = admin_client.get("/db/admin/models")
    assert response.status_code == 200
    data = response.json()
    assert "activeModel" in data

def test_set_active_model(admin_client):
    response = admin_client.put(
        "/db/admin/models/active",
        json={"activeModel": "gemini-test-pro"}
    )
    assert response.status_code == 200
    assert response.json() == {"message": "Active model set to gemini-test-pro"}
    
    # Verify it updated
    response2 = admin_client.get("/db/admin/models")
    assert response2.json()["activeModel"] == "gemini-test-pro"
