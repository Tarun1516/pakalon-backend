"""Tests for model registry and /models endpoints (T042)."""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.services.model_registry import (
    _classify_model,
    cache_models,
    get_models_for_plan,
    pick_auto_model,
)

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

SAMPLE_FREE_MODEL = {
    "id": "deepseek/deepseek-r1:free",
    "name": "DeepSeek R1 (free)",
    "context_length": 128_000,
    "pricing": {"prompt": "0", "completion": "0"},
}

SAMPLE_PAID_MODEL = {
    "id": "anthropic/claude-3.5-sonnet",
    "name": "Claude 3.5 Sonnet",
    "context_length": 200_000,
    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
}


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests — _classify_model
# ──────────────────────────────────────────────────────────────────────────────

def test_classify_model_free_by_id_suffix():
    assert _classify_model(SAMPLE_FREE_MODEL) == "free"


def test_classify_model_free_by_zero_pricing():
    model = {"id": "some/model", "pricing": {"prompt": "0", "completion": "0"}}
    assert _classify_model(model) == "free"


def test_classify_model_paid():
    assert _classify_model(SAMPLE_PAID_MODEL) == "paid"


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests — pick_auto_model
# ──────────────────────────────────────────────────────────────────────────────

def test_pick_auto_model_free_plan_picks_highest_context():
    models = [
        {**SAMPLE_FREE_MODEL, "context_length": 32_000, "tier": "free"},
        {**SAMPLE_FREE_MODEL, "id": "other/model:free", "context_length": 128_000, "tier": "free"},
    ]
    result = pick_auto_model("free", models)
    assert result["context_length"] == 128_000


def test_pick_auto_model_pro_prefers_claude():
    models = [
        {**SAMPLE_FREE_MODEL, "id": "deepseek/deepseek:free", "tier": "free"},
        {**SAMPLE_PAID_MODEL, "id": "anthropic/claude-3.5-sonnet", "tier": "paid"},
    ]
    result = pick_auto_model("pro", models)
    assert "claude" in result["id"]


def test_pick_auto_model_empty_returns_none():
    assert pick_auto_model("free", []) is None


# ──────────────────────────────────────────────────────────────────────────────
# Integration tests — cache_models + get_models_for_plan
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_and_retrieve_models(db_session):
    models = [SAMPLE_FREE_MODEL, SAMPLE_PAID_MODEL]
    await cache_models(models, db_session)
    await db_session.flush()

    free_models = await get_models_for_plan("free", db_session)
    assert any(m["id"] == SAMPLE_FREE_MODEL["id"] for m in free_models)
    assert not any(m["id"] == SAMPLE_PAID_MODEL["id"] for m in free_models)


@pytest.mark.asyncio
async def test_pro_plan_gets_all_models(db_session):
    models = [SAMPLE_FREE_MODEL, SAMPLE_PAID_MODEL]
    await cache_models(models, db_session)
    await db_session.flush()

    pro_models = await get_models_for_plan("pro", db_session)
    ids = [m["id"] for m in pro_models]
    assert SAMPLE_FREE_MODEL["id"] in ids
    assert SAMPLE_PAID_MODEL["id"] in ids


@pytest.mark.asyncio
async def test_cached_model_payload_round_trips_json_fields(db_session):
    await cache_models([SAMPLE_FREE_MODEL], db_session)
    await db_session.commit()

    free_models = await get_models_for_plan("free", db_session)
    assert free_models[0]["pricing"] == SAMPLE_FREE_MODEL["pricing"]


@pytest.mark.asyncio
async def test_free_plan_can_request_full_catalog(db_session):
    models = [SAMPLE_FREE_MODEL, SAMPLE_PAID_MODEL]
    await cache_models(models, db_session)
    await db_session.flush()

    all_models = await get_models_for_plan("free", db_session, include_all=True)
    ids = [m["id"] for m in all_models]
    assert SAMPLE_FREE_MODEL["id"] in ids
    assert SAMPLE_PAID_MODEL["id"] in ids


@pytest.mark.asyncio
async def test_legacy_model_cache_schema_is_upgraded_in_place():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE model_cache (
                id TEXT PRIMARY KEY,
                model_id VARCHAR(255) NOT NULL UNIQUE,
                name VARCHAR(500) NOT NULL DEFAULT '',
                provider VARCHAR(50),
                context_window INTEGER NOT NULL DEFAULT 0,
                pricing_tier VARCHAR(20) NOT NULL DEFAULT 'pro',
                supports_tools BOOLEAN NOT NULL DEFAULT 0,
                raw_json TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """))

    async with session_factory() as session:
        await cache_models([SAMPLE_FREE_MODEL], session)
        await session.commit()

        free_models = await get_models_for_plan("free", session)
        assert [model["id"] for model in free_models] == [SAMPLE_FREE_MODEL["id"]]
        assert free_models[0]["context_length"] == SAMPLE_FREE_MODEL["context_length"]
        assert free_models[0]["tier"] == "free"
        assert free_models[0]["pricing"] == SAMPLE_FREE_MODEL["pricing"]

        row = (
            await session.execute(
                text(
                    "SELECT context_length, tier, fetched_at, cache_valid "
                    "FROM model_cache WHERE model_id = :model_id"
                ),
                {"model_id": SAMPLE_FREE_MODEL["id"]},
            )
        ).one()
        assert row.context_length == SAMPLE_FREE_MODEL["context_length"]
        assert row.tier == "free"
        assert row.fetched_at is not None
        assert row.cache_valid in (1, True)

    await engine.dispose()


# ──────────────────────────────────────────────────────────────────────────────
# HTTP tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_models_requires_auth(client):
    response = await client.get("/models")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_list_models_authenticated(client, free_user):
    from tests.conftest import make_jwt_for_user
    token = make_jwt_for_user(free_user)
    response = await client.get("/models", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert "models" in response.json()


@pytest.mark.asyncio
async def test_list_models_include_all_returns_full_catalog(client, free_user, db_session):
    from tests.conftest import make_jwt_for_user

    await cache_models([SAMPLE_FREE_MODEL, SAMPLE_PAID_MODEL], db_session)
    await db_session.commit()

    token = make_jwt_for_user(free_user)
    response = await client.get(
        "/models?include_all=true",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    ids = [model["id"] for model in response.json()["models"]]
    assert SAMPLE_FREE_MODEL["id"] in ids
    assert SAMPLE_PAID_MODEL["id"] in ids


@pytest.mark.asyncio
async def test_model_context_status_accepts_slash_model_ids(client, free_user):
    from tests.conftest import make_jwt_for_user

    token = make_jwt_for_user(free_user)
    response = await client.get(
        f"/models/{SAMPLE_FREE_MODEL['id']}/context",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["model_id"] == SAMPLE_FREE_MODEL["id"]
    assert response.json()["exhausted"] is False


@pytest.mark.asyncio
async def test_auto_model_503_if_empty_cache(client, free_user, db_session):
    from tests.conftest import make_jwt_for_user
    from app.models.model_cache import ModelCache
    from sqlalchemy import delete

    await db_session.execute(delete(ModelCache))
    await db_session.commit()

    token = make_jwt_for_user(free_user)
    response = await client.get("/models/auto", headers={"Authorization": f"Bearer {token}"})
    # Empty model cache → 503
    assert response.status_code == 503
