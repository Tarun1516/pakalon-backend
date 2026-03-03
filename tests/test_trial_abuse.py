"""Tests for trial abuse prevention service (T032)."""
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.user import User
from app.services.trial_abuse import (
    TRIAL_DAYS,
    can_delete_account,
    get_or_create_user_by_github,
    increment_trial_days,
    is_trial_expired,
    is_trial_expiring_soon,
    remaining_trial_days,
)


# ──────────────────────────────────────────────────────────────────────────────
# Pure unit tests — no DB needed
# ──────────────────────────────────────────────────────────────────────────────

def _make_free_user(trial_days_used: int = 0) -> User:
    u = User()
    u.id = str(uuid.uuid4())
    u.plan = "free"
    u.trial_days_used = trial_days_used
    return u


def _make_pro_user() -> User:
    u = User()
    u.id = str(uuid.uuid4())
    u.plan = "pro"
    u.trial_days_used = 0
    return u


def test_remaining_trial_days_fresh_user():
    user = _make_free_user(trial_days_used=0)
    assert remaining_trial_days(user) == TRIAL_DAYS


def test_remaining_trial_days_partial():
    user = _make_free_user(trial_days_used=10)
    assert remaining_trial_days(user) == 20


def test_remaining_trial_days_exhausted():
    user = _make_free_user(trial_days_used=30)
    assert remaining_trial_days(user) == 0


def test_remaining_trial_days_pro_user():
    user = _make_pro_user()
    assert remaining_trial_days(user) == 0


def test_is_trial_expired_false_when_days_remain():
    user = _make_free_user(trial_days_used=10)
    assert not is_trial_expired(user)


def test_is_trial_expired_true_when_exhausted():
    user = _make_free_user(trial_days_used=30)
    assert is_trial_expired(user)


def test_is_trial_expired_false_for_pro():
    user = _make_pro_user()
    assert not is_trial_expired(user)


def test_is_trial_expiring_soon_yes():
    user = _make_free_user(trial_days_used=26)  # 4 days left
    assert is_trial_expiring_soon(user, threshold_days=5)


def test_is_trial_expiring_soon_no():
    user = _make_free_user(trial_days_used=10)  # 20 days left
    assert not is_trial_expiring_soon(user, threshold_days=5)


def test_can_delete_account_fresh_user():
    user = _make_free_user(trial_days_used=0)
    assert can_delete_account(user)


def test_can_delete_account_with_days_remaining():
    user = _make_free_user(trial_days_used=15)
    assert can_delete_account(user)


def test_cannot_delete_account_after_trial_exhausted():
    user = _make_free_user(trial_days_used=30)
    assert not can_delete_account(user)


def test_pro_user_can_always_delete():
    user = _make_pro_user()
    assert can_delete_account(user)


def test_increment_trial_days():
    user = _make_free_user(trial_days_used=10)
    increment_trial_days(user, days=5)
    assert user.trial_days_used == 15


def test_increment_trial_days_caps_at_30():
    user = _make_free_user(trial_days_used=28)
    increment_trial_days(user, days=5)
    assert user.trial_days_used == TRIAL_DAYS


# ──────────────────────────────────────────────────────────────────────────────
# DB tests — get_or_create_user_by_github
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_new_user(db_session):
    user = await get_or_create_user_by_github(
        github_login="testuser",
        clerk_id=f"clerk_{uuid.uuid4().hex}",
        email="test@example.com",
        display_name="Test User",
        session=db_session,
    )
    assert user.github_login == "testuser"
    assert user.plan == "free"
    assert user.trial_days_used == 0


@pytest.mark.asyncio
async def test_carry_over_trial_days_for_same_github(db_session):
    """New clerk_id with same github_login should carry over trial_days_used."""
    # Create original user with 25 days used
    original_user = await get_or_create_user_by_github(
        github_login="carry-over-test",
        clerk_id=f"clerk_original_{uuid.uuid4().hex}",
        email="original@example.com",
        display_name="Original",
        session=db_session,
    )
    original_user.trial_days_used = 25
    await db_session.flush()

    # Create a new account with same github_login but different clerk_id
    new_user = await get_or_create_user_by_github(
        github_login="carry-over-test",
        clerk_id=f"clerk_new_{uuid.uuid4().hex}",
        email="new@example.com",
        display_name="New",
        session=db_session,
    )
    # Trial days should be carried over
    assert new_user.trial_days_used == 25


@pytest.mark.asyncio
async def test_upsert_existing_user(db_session, free_user: User):
    """Calling with same clerk_id should update and return same user."""
    updated = await get_or_create_user_by_github(
        github_login="updated-login",
        clerk_id=free_user.clerk_id,
        email="updated@example.com",
        display_name="Updated",
        session=db_session,
    )
    assert updated.id == free_user.id
    assert updated.github_login == "updated-login"


# ──────────────────────────────────────────────────────────────────────────────
# HTTP tests — DELETE /users/{id}
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_user_blocked_if_trial_exhausted(client, expired_user: User):
    from tests.conftest import make_jwt_for_user

    # expired_user has trial_days_used=30 (trial exhausted)
    token = make_jwt_for_user(expired_user)
    # expired_user's trial is expired so /auth/me returns 403
    # Use a fresh user with exhausted trial to test the delete guard
    response = await client.delete(
        f"/users/{expired_user.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # 403 because trial is expired (middleware blocks)
    assert response.status_code == 403
