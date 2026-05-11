import asyncio
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.database import get_db
from app.main import app
from app.auth.azure_ad import create_dev_token
from app.config import get_settings

settings = get_settings()

TEST_DB_URL = "sqlite+aiosqlite:///./data/test_pm.db"

test_engine = create_async_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session")
async def setup_db():
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed test data
    async with TestSession() as db:
        from app.core.pm_library import seed_from_json
        await seed_from_json(db)

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def db_session(setup_db):
    async with TestSession() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session):
    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    manager_token = create_dev_token("test-manager", "manager@test.com", "Test Manager", ["Manager"])
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.headers["Authorization"] = f"Bearer {manager_token}"
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def manager_token():
    return create_dev_token("test-manager", "manager@test.com", "Test Manager", ["Manager"])


@pytest.fixture
def technician_token():
    return create_dev_token("test-tech", "tech@test.com", "Test Tech", ["Technician"])


@pytest.fixture
def supervisor_token():
    return create_dev_token("test-sup", "sup@test.com", "Test Supervisor", ["Supervisor"])
