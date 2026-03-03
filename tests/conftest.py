"""Pytest configuration and shared fixtures for Pakalon backend tests."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, get_session
from app.models.user import User
from app.models.subscription import Subscription


# ──────────────────────────────────────────────────────────────────────────────
# Test Database Setup (SQLite in-memory for speed)
# ──────────────────────────────────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="session")
async def engine():
    """Create a shared in-memory SQLite engine for the test session."""
    _engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield _engine
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _engine.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:
    """Yield a test DB session that rolls back after each test."""
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()


# ──────────────────────────────────────────────────────────────────────────────
# Redis Mock
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    """Return a fake Redis instance that resets between tests."""
    redis = FakeRedis()
    yield redis
    await redis.flushall()
    await redis.aclose()


# ──────────────────────────────────────────────────────────────────────────────
# Sample User Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def free_user(db_session: AsyncSession) -> User:
    """A free-plan user with an active trial (10 days used)."""
    user = User(
        id=str(uuid.uuid4()),
        clerk_id="clerk_free_user_test",
        github_login="free-test-user",
        email="free@test.example",
        display_name="Free Test User",
        plan="free",
        trial_start=datetime.now(tz=timezone.utc) - timedelta(days=10),
        trial_end=datetime.now(tz=timezone.utc) + timedelta(days=20),
        trial_days_used=10,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def pro_user(db_session: AsyncSession) -> User:
    """A pro-plan user with an active Polar subscription."""
    user = User(
        id=str(uuid.uuid4()),
        clerk_id="clerk_pro_user_test",
        github_login="pro-test-user",
        email="pro@test.example",
        display_name="Pro Test User",
        plan="pro",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    subscription = Subscription(
        id=str(uuid.uuid4()),
        user_id=user.id,
        polar_sub_id="polar_sub_test_001",
        status="active",
        period_start=datetime.now(tz=timezone.utc) - timedelta(days=5),
        period_end=datetime.now(tz=timezone.utc) + timedelta(days=25),
        grace_end=datetime.now(tz=timezone.utc) + timedelta(days=28),
        amount_usd=22.00,
    )
    db_session.add(subscription)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def expired_user(db_session: AsyncSession) -> User:
    """A free-plan user whose trial has expired."""
    user = User(
        id=str(uuid.uuid4()),
        clerk_id="clerk_expired_user_test",
        github_login="expired-test-user",
        email="expired@test.example",
        display_name="Expired Test User",
        plan="free",
        trial_start=datetime.now(tz=timezone.utc) - timedelta(days=35),
        trial_end=datetime.now(tz=timezone.utc) - timedelta(days=5),
        trial_days_used=30,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ──────────────────────────────────────────────────────────────────────────────
# HTTP Test Client
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db_session: AsyncSession, fake_redis) -> AsyncGenerator[AsyncClient, None]:
    """AsyncClient with dependency overrides for DB and Redis."""
    from app.main import app

    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c

    app.dependency_overrides.clear()


def make_jwt_for_user(user: User) -> str:
    """Helper to generate a valid Pakalon JWT for a test user."""
    import jwt
    from app.config import get_settings

    settings = get_settings()
    payload = {
        "sub": user.id,
        "iat": datetime.now(tz=timezone.utc),
        "exp": datetime.now(tz=timezone.utc) + timedelta(days=90),
        "plan": user.plan,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
