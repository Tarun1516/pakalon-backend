"""Tests for dashboard stats aggregation and rendering contracts."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.login_event import LoginEvent
from app.models.message import Message
from app.models.model_usage import ModelUsage
from app.models.session import Session as PakalonSession
from app.models.user import User


@pytest.mark.asyncio
async def test_dashboard_sessions_prefer_actual_user_prompt(
    client,
    free_user: User,
    db_session: AsyncSession,
):
    """Dashboard should show the first user prompt, not a generic session title."""
    from tests.conftest import make_jwt_for_user

    session_id = str(uuid.uuid4())
    created_at = datetime.now(tz=timezone.utc) - timedelta(days=1)

    chat_session = PakalonSession(
        id=session_id,
        user_id=free_user.id,
        title="New Chat",
        model_id="openrouter/test-model:free",
        created_at=created_at,
    )
    db_session.add(chat_session)

    db_session.add(
        Message(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role="assistant",
            content="Hello!",
            created_at=created_at + timedelta(seconds=1),
        )
    )
    db_session.add(
        Message(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role="user",
            content="Build me a secure auth middleware\nwith JWT rotation.",
            created_at=created_at + timedelta(seconds=2),
        )
    )
    await db_session.commit()

    token = make_jwt_for_user(free_user)
    response = await client.get(
        "/dashboard/stats?days=30",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    sessions = response.json()["sessions"]
    item = next((row for row in sessions if row["id"] == session_id), None)
    assert item is not None
    assert item["prompt_text"] == "Build me a secure auth middleware with JWT rotation."


@pytest.mark.asyncio
async def test_dashboard_login_history_is_ascending_and_has_account_created(
    client,
    free_user: User,
    db_session: AsyncSession,
):
    """Dashboard login history should be oldest-first and include account-created marker."""
    from tests.conftest import make_jwt_for_user

    free_user.created_at = datetime.now(tz=timezone.utc) - timedelta(days=10)

    first = LoginEvent(
        id=str(uuid.uuid4()),
        user_id=free_user.id,
        login_type="web",
        browser="Chrome",
        os="Windows",
        machine_id="machine-a",
        created_at=datetime.now(tz=timezone.utc) - timedelta(days=7),
    )
    second = LoginEvent(
        id=str(uuid.uuid4()),
        user_id=free_user.id,
        login_type="device_code",
        browser="Terminal",
        os="Linux",
        machine_id="machine-b",
        created_at=datetime.now(tz=timezone.utc) - timedelta(days=2),
    )

    db_session.add_all([first, second])
    await db_session.commit()

    token = make_jwt_for_user(free_user)
    response = await client.get(
        "/dashboard/stats?days=30",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    login_events = response.json()["login_events"]
    assert len(login_events) >= 3
    assert login_events[0]["login_type"] == "account_created"

    timestamps = [
        datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
        for event in login_events
        if event.get("created_at")
    ]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_dashboard_monthly_tokens_aggregate_from_account_creation(
    client,
    free_user: User,
    db_session: AsyncSession,
):
    """Dashboard should provide month-wise token totals from account creation onward."""
    from tests.conftest import make_jwt_for_user

    now = datetime.now(tz=timezone.utc)
    month_one = now - timedelta(days=65)
    month_two = now - timedelta(days=15)

    free_user.created_at = month_one - timedelta(days=3)

    db_session.add(
        ModelUsage(
            id=str(uuid.uuid4()),
            user_id=free_user.id,
            model_id="openrouter/alpha:free",
            tokens_used=125,
            context_window_size=8000,
            context_window_used=100,
            created_at=month_one,
        )
    )
    db_session.add(
        ModelUsage(
            id=str(uuid.uuid4()),
            user_id=free_user.id,
            model_id="openrouter/beta:free",
            tokens_used=275,
            context_window_size=8000,
            context_window_used=200,
            created_at=month_two,
        )
    )
    await db_session.commit()

    token = make_jwt_for_user(free_user)
    response = await client.get(
        "/dashboard/stats?days=365",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()

    month_one_key = f"{month_one.year:04d}-{month_one.month:02d}"
    month_two_key = f"{month_two.year:04d}-{month_two.month:02d}"
    monthly_tokens = {entry["month"]: entry["tokens"] for entry in payload["monthly_tokens"]}

    assert month_one_key in monthly_tokens
    assert month_two_key in monthly_tokens
    assert monthly_tokens[month_one_key] >= 125
    assert monthly_tokens[month_two_key] >= 275
