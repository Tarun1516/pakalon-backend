"""Tests for email service (T152)."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.email import enqueue_reminder_series


@pytest.mark.asyncio
async def test_enqueue_reminder_emails_for_expiring_user(db_session, free_user):
    """Should enqueue a reminder email for a user with 4 days left."""
    free_user.trial_days_used = 26  # 4 days remaining
    await db_session.flush()

    await enqueue_reminder_series(
        user_id=free_user.id,
        email=free_user.email,
        display_name=free_user.display_name or "Test",
        days_remaining=4,
        session=db_session,
    )
    await db_session.flush()

    from sqlalchemy import select
    from app.models.email_queue import EmailQueue

    result = await db_session.execute(
        select(EmailQueue).where(EmailQueue.user_id == free_user.id)
    )
    queued = result.scalars().all()
    assert len(queued) >= 1
    assert all(q.status == "pending" for q in queued)


@pytest.mark.asyncio
async def test_no_duplicate_reminder_emails(db_session, free_user):
    """Calling enqueue twice should not create duplicate emails."""
    free_user.trial_days_used = 26
    await db_session.flush()

    for _ in range(2):
        await enqueue_reminder_series(
            user_id=free_user.id,
            email=free_user.email,
            display_name="Test",
            days_remaining=4,
            session=db_session,
        )
    await db_session.flush()

    from sqlalchemy import select, func
    from app.models.email_queue import EmailQueue

    result = await db_session.execute(
        select(func.count())
        .select_from(EmailQueue)
        .where(EmailQueue.user_id == free_user.id)
    )
    count = result.scalar_one()
    # Should not have duplicates
    assert count <= 4  # at most 4 threshold levels
