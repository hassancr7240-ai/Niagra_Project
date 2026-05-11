import pytest
from httpx import ASGITransport, AsyncClient

from app.auth.azure_ad import create_dev_token
from app.auth.rbac import CurrentUser, Role, normalize_role
from app.main import app

transport = ASGITransport(app=app)


@pytest.mark.asyncio
async def test_unauthenticated_request_returns_401():
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/api/machines")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_returns_401():
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["Authorization"] = "Bearer invalid-token-here"
        response = await ac.get("/api/machines")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_dev_token_authenticated():
    token = create_dev_token("u1", "user@test.com", "Test User", ["Manager"])
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["Authorization"] = f"Bearer {token}"
        response = await ac.get("/api/machines")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_dev_api_key_works():
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["X-API-Key"] = "dev-secret-key-change-in-prod"
        response = await ac.get("/api/machines")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_technician_cannot_write_library():
    token = create_dev_token("tech1", "tech@test.com", "Tech User", ["Technician"])
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["Authorization"] = f"Bearer {token}"
        response = await ac.post(
            "/api/library/tasks",
            json={"machine_id": "CONTIFORM-C3-L3", "interval_hours": 8, "tasks": []},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_supervisor_cannot_generate():
    token = create_dev_token("sup1", "sup@test.com", "Supervisor", ["Supervisor"])
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["Authorization"] = f"Bearer {token}"
        response = await ac.post(
            "/api/generate",
            json={"machine_id": "CONTIFORM-C3-L3", "interval_hours": 8,
                  "work_order": "WO-X", "technician_name": "Test"},
        )
    assert response.status_code == 403


def test_normalize_role_manager():
    assert normalize_role(["Manager"]) == Role.MANAGER


def test_normalize_role_case_insensitive():
    assert normalize_role(["TECHNICIAN"]) == Role.TECHNICIAN


def test_normalize_role_priority():
    # Manager takes priority over Technician
    assert normalize_role(["Technician", "Manager"]) == Role.MANAGER


def test_normalize_role_unknown_defaults_to_technician():
    assert normalize_role(["UnknownRole"]) == Role.TECHNICIAN


def test_current_user_permission_check():
    user = CurrentUser(user_id="u1", email="u@test.com", name="Test", role="Manager")
    assert user.can("library:write")
    assert user.can("pm:generate")
    assert user.can("pm:approve")


def test_technician_permission_check():
    user = CurrentUser(user_id="u2", email="t@test.com", name="Tech", role="Technician")
    assert user.can("pm:generate")
    assert not user.can("library:write")
    assert not user.can("pm:approve")


def test_health_check_no_auth():
    import asyncio
    async def _check():
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.get("/health")
        return r
    response = asyncio.get_event_loop().run_until_complete(_check())
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
