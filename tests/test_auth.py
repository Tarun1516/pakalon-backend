"""Tests for device code auth flow (T031)."""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select

from app.models.device_code import DeviceCode
from app.models.user import User
from app.schemas.auth import DeviceCodeConfirmRequest
from app.services.device_code import (
    DEVICE_CODE_ALPHABET,
    DEVICE_CODE_TTL_SECONDS,
    confirm_code,
    create_device_code,
    generate_code,
    issue_jwt,
    poll_status,
)
from tests.conftest import make_jwt_for_user


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests — generate_code
# ──────────────────────────────────────────────────────────────────────────────

def test_generate_code_is_6_char_pakalon_code():
    code = generate_code()
    assert len(code) == 6
    assert all(ch in DEVICE_CODE_ALPHABET for ch in code)


def test_generate_code_different_each_time():
    codes = {generate_code() for _ in range(100)}
    # Should have at least ~90 unique codes out of 100 (very conservative)
    assert len(codes) > 50


# ──────────────────────────────────────────────────────────────────────────────
# Unit tests — issue_jwt
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_issue_jwt_contains_sub_and_plan(free_user: User):
    import jwt
    from app.config import get_settings
    settings = get_settings()
    token = issue_jwt(free_user)
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == free_user.id
    assert payload["plan"] == free_user.plan


# ──────────────────────────────────────────────────────────────────────────────
# Integration tests — create_device_code
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_device_code(db_session, fake_redis):
    device_id = str(uuid.uuid4())
    dc, is_first_machine_run, launch_experience = await create_device_code(
        device_id=device_id,
        machine_id="test-machine",
        session=db_session,
        redis=fake_redis,
    )
    assert dc.device_id == device_id
    assert dc.status == "pending"
    assert len(dc.code) == 6
    assert _as_utc(dc.expires_at) > datetime.now(tz=timezone.utc)
    assert isinstance(is_first_machine_run, bool)
    assert launch_experience in {"video", "text"}

    # Redis should have the code
    cached = await fake_redis.get(f"device_code:{device_id}")
    assert cached is not None


@pytest.mark.asyncio
async def test_create_device_code_expires_existing_pending(db_session, fake_redis):
    device_id = str(uuid.uuid4())
    # Create first code
    dc1, _, _ = await create_device_code(
        device_id=device_id,
        machine_id=None,
        session=db_session,
        redis=fake_redis,
    )
    # Create second code for same device_id
    dc2, _, _ = await create_device_code(
        device_id=device_id,
        machine_id=None,
        session=db_session,
        redis=fake_redis,
    )
    result = await db_session.execute(
        select(DeviceCode).where(DeviceCode.device_id == device_id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].id == dc2.id
    assert rows[0].status == "pending"
    assert rows[0].id != dc1.id


# ──────────────────────────────────────────────────────────────────────────────
# Integration tests — poll_status
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_status_pending(db_session, fake_redis):
    device_id = str(uuid.uuid4())
    await create_device_code(
        device_id=device_id,
        machine_id=None,
        session=db_session,
        redis=fake_redis,
    )
    result = await poll_status(device_id=device_id, session=db_session, redis=fake_redis)
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_poll_status_approved_from_cached_payload(db_session, fake_redis, free_user: User):
    device_id = str(uuid.uuid4())
    dc, _, _ = await create_device_code(
        device_id=device_id,
        machine_id=None,
        session=db_session,
        redis=fake_redis,
    )
    dc.status = "approved"
    dc.user_id = free_user.id
    dc.approved_at = datetime.now(tz=timezone.utc)
    await db_session.commit()

    cached_payload = {
        "status": "approved",
        "token": "cached-token",
        "user_id": free_user.id,
        "plan": free_user.plan,
        "trial_days_remaining": 20,
        "trial_ends_at": (datetime.now(tz=timezone.utc) + timedelta(days=20)).date().isoformat(),
    }
    await fake_redis.setex(f"device_token:{device_id}", DEVICE_CODE_TTL_SECONDS, json.dumps(cached_payload))

    result = await poll_status(device_id=device_id, session=db_session, redis=fake_redis)
    assert result == cached_payload


@pytest.mark.asyncio
async def test_poll_status_legacy_cached_token_falls_back_to_db(db_session, fake_redis, free_user: User):
    device_id = str(uuid.uuid4())
    dc, _, _ = await create_device_code(
        device_id=device_id,
        machine_id=None,
        session=db_session,
        redis=fake_redis,
    )
    dc.status = "approved"
    dc.user_id = free_user.id
    dc.approved_at = datetime.now(tz=timezone.utc)
    await db_session.commit()

    await fake_redis.setex(f"device_token:{device_id}", DEVICE_CODE_TTL_SECONDS, "legacy-cached-token")

    result = await poll_status(device_id=device_id, session=db_session, redis=fake_redis)
    assert result["status"] == "approved"
    assert result["user_id"] == free_user.id
    assert result["plan"] == free_user.plan
    assert result["token"]


@pytest.mark.asyncio
async def test_poll_status_expired_not_found(db_session, fake_redis):
    result = await poll_status(
        device_id="nonexistent-device",
        session=db_session,
        redis=fake_redis,
    )
    assert result["status"] == "expired"


@pytest.mark.asyncio
async def test_confirm_code_rejects_invalid_format(db_session, fake_redis):
    device_id = str(uuid.uuid4())
    await create_device_code(
        device_id=device_id,
        machine_id="test-machine",
        session=db_session,
        redis=fake_redis,
    )

    with pytest.raises(ValueError, match="Code must be 6 letters/numbers"):
        await confirm_code(
            device_id=device_id,
            code="12AB0O",
            supabase_user_id="supabase_test_user",
            github_login="test-user",
            email="test@example.com",
            display_name="Test User",
            session=db_session,
            redis=fake_redis,
        )


@pytest.mark.asyncio
async def test_confirm_code_rejects_mismatched_code(db_session, fake_redis, monkeypatch):
    from app.services import device_code as device_code_module

    class _NaiveDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.utcnow()

    monkeypatch.setattr(device_code_module, "datetime", _NaiveDateTime)

    device_id = str(uuid.uuid4())
    dc, _, _ = await create_device_code(
        device_id=device_id,
        machine_id="test-machine",
        session=db_session,
        redis=fake_redis,
    )
    wrong_code = "AAAAAA" if dc.code != "AAAAAA" else "BBBBBB"

    with pytest.raises(ValueError, match="Invalid or mismatched device code"):
        await confirm_code(
            device_id=device_id,
            code=wrong_code,
            supabase_user_id="supabase_test_user",
            github_login="test-user",
            email="test@example.com",
            display_name="Test User",
            session=db_session,
            redis=fake_redis,
        )


# ──────────────────────────────────────────────────────────────────────────────
# HTTP tests — POST /auth/devices
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_device_code_endpoint(client):
    device_id = str(uuid.uuid4())
    response = await client.post(
        "/auth/devices",
        json={"device_id": device_id, "machine_id": "test-machine"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["device_id"] == device_id
    assert "code" in data
    assert data["expires_in"] == DEVICE_CODE_TTL_SECONDS


@pytest.mark.asyncio
async def test_poll_device_token_pending(client):
    # First create a device code
    device_id = str(uuid.uuid4())
    await client.post(
        "/auth/devices",
        json={"device_id": device_id},
    )
    response = await client.get(f"/auth/devices/{device_id}/token")
    assert response.status_code == 200
    assert response.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_poll_device_token_expired(client):
    response = await client.get("/auth/devices/nonexistent-id/token")
    assert response.status_code == 410


def test_confirm_request_schema_requires_exactly_6_digits():
    valid = DeviceCodeConfirmRequest(code="ABC234")
    assert valid.code == "ABC234"

    with pytest.raises(ValidationError):
        DeviceCodeConfirmRequest(code="12345")

    with pytest.raises(ValidationError):
        DeviceCodeConfirmRequest(code="AB10I6")


# ──────────────────────────────────────────────────────────────────────────────
# HTTP tests — authenticated endpoints
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_me_authenticated(client, free_user: User):
    token = make_jwt_for_user(free_user)
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == free_user.id
    assert data["plan"] == "free"


@pytest.mark.asyncio
async def test_get_me_unauthenticated(client):
    response = await client.get("/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_me_expired_user_forbidden(client, expired_user: User):
    token = make_jwt_for_user(expired_user)
    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    # Middleware should block expired trial users
    assert response.status_code == 403
