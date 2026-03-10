"""Regression tests for credit balance and startup credit gating."""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.credit_ledger import CreditLedger


@pytest.mark.asyncio
async def test_startup_check_allows_free_user_with_zero_credits(client, free_user):
    """Free users should be able to open the app even though their ledger total is 0."""
    from tests.conftest import make_jwt_for_user

    token = make_jwt_for_user(free_user)
    response = await client.get(
        "/credits/startup-check",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["plan"] == "free"
    assert data["credits_remaining"] == 0
    assert data["can_interact"] is True
    assert data["reason"] is None


@pytest.mark.asyncio
async def test_startup_check_blocks_credit_bearing_plan_with_no_remaining_credits(
    client,
    pro_user,
    db_session,
):
    """Paid plans should still be blocked once their monthly credits are exhausted."""
    from tests.conftest import make_jwt_for_user

    now = datetime.now(tz=timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if now.month == 12:
        period_end = period_start.replace(year=now.year + 1, month=1)
    else:
        period_end = period_start.replace(month=now.month + 1)

    db_session.add(
        CreditLedger(
            id=str(uuid.uuid4()),
            user_id=pro_user.id,
            plan="pro",
            credits_total=500,
            credits_used=500,
            period_start=period_start,
            period_end=period_end,
        )
    )
    await db_session.commit()

    token = make_jwt_for_user(pro_user)
    response = await client.get(
        "/credits/startup-check",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["plan"] == "pro"
    assert data["credits_remaining"] == 0
    assert data["can_interact"] is False
    assert "no credits remaining" in data["reason"].lower()