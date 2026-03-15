"""Tests for billing endpoints and service (T151)."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.subscription import Subscription
from app.models.user import User
from app.services.billing import (
    handle_polar_subscription_activated,
    handle_polar_subscription_revoked,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_checkout_requires_auth(client):
    response = await client.post("/billing/checkout", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cancel_requires_pro(client, free_user: User):
    from tests.conftest import make_jwt_for_user
    token = make_jwt_for_user(free_user)
    response = await client.delete("/billing/cancel", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_subscription_status(client, pro_user: User):
    from tests.conftest import make_jwt_for_user
    token = make_jwt_for_user(pro_user)
    response = await client.get("/billing/subscription", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_polar_subscription_activated_upgrades_user(db_session, free_user):
    """Receiving subscription.activated should upgrade the user to pro."""
    payload = {
        "type": "subscription.activated",
        "data": {
            "id": f"polar_sub_{uuid.uuid4().hex}",
            "metadata": {"pakalon_user_id": free_user.id},
            "current_period_end": (
                datetime.now(tz=timezone.utc) + timedelta(days=30)
            ).isoformat(),
        },
    }
    await handle_polar_subscription_activated(payload, db_session)
    await db_session.refresh(free_user)
    assert free_user.plan == "pro"


@pytest.mark.asyncio
async def test_polar_subscription_revoked_sets_grace_period(db_session, pro_user):
    """subscription.revoked should set status=past_due with a grace period."""
    # Create an active subscription first
    sub = Subscription(
        id=str(uuid.uuid4()),
        user_id=pro_user.id,
        polar_sub_id=f"polar_sub_{uuid.uuid4().hex}",
        status="active",
        created_at=datetime.now(tz=timezone.utc),
    )
    db_session.add(sub)
    await db_session.flush()

    payload = {
        "type": "subscription.revoked",
        "data": {"id": sub.polar_sub_id},
    }
    await handle_polar_subscription_revoked(payload, db_session)
    await db_session.refresh(sub)
    assert sub.status == "past_due"
    assert sub.grace_end is not None
    assert _as_utc(sub.grace_end) > datetime.now(tz=timezone.utc)
