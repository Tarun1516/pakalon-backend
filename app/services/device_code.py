"""Device code service — core 6-digit auth flow."""
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.device_code import DeviceCode
from app.models.user import User

logger = logging.getLogger(__name__)

DEVICE_CODE_TTL_SECONDS = 600  # 10 minutes


def generate_code() -> str:
    """Generate a cryptographically random 6-digit numeric code."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def create_device_code(
    device_id: str,
    machine_id: str | None,
    session: AsyncSession,
    redis=None,
) -> DeviceCode:
    """
    Create a new device code record in PostgreSQL and store in Redis with TTL.

    Returns the newly created DeviceCode.
    """
    # Check if an active code already exists for this device_id
    existing = await session.execute(
        select(DeviceCode).where(
            DeviceCode.device_id == device_id,
            DeviceCode.status == "pending",
        )
    )
    existing_code = existing.scalar_one_or_none()
    if existing_code:
        # Delete the old pending code so the unique constraint on device_id
        # is freed before we insert the replacement row.
        await session.delete(existing_code)
        await session.flush()

    code = generate_code()
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=DEVICE_CODE_TTL_SECONDS)

    device_code = DeviceCode(
        id=str(uuid.uuid4()),
        device_id=device_id,
        code=code,
        machine_id=machine_id,
        expires_at=expires_at,
        status="pending",
    )
    session.add(device_code)
    await session.flush()
    await session.refresh(device_code)

    # Cache in Redis for fast polling
    if redis is not None:
        redis_key = f"device_code:{device_id}"
        await redis.setex(
            redis_key,
            DEVICE_CODE_TTL_SECONDS,
            code,
        )

    return device_code


async def confirm_code(
    device_id: str,
    code: str,
    clerk_user_id: str,
    github_login: str | None,
    email: str | None,
    display_name: str | None,
    session: AsyncSession,
    redis=None,
) -> tuple[DeviceCode, User]:
    """
    Confirm a device code from the website (user has authenticated via Clerk).

    Returns (device_code, user) on success.
    Raises ValueError for invalid/expired codes.
    """
    # Enforce strict 6-digit code format server-side
    normalized_code = (code or "").strip()
    if len(normalized_code) != 6 or not normalized_code.isdigit():
        raise ValueError("Invalid code format. Code must be 6 digits")

    # Look up the pending device code
    result = await session.execute(
        select(DeviceCode).where(
            DeviceCode.device_id == device_id,
            DeviceCode.status == "pending",
        )
    )
    device_code = result.scalar_one_or_none()

    if device_code is None:
        raise ValueError("Device code not found or already used")

    if datetime.now(tz=timezone.utc) > device_code.expires_at:
        device_code.status = "expired"
        await session.flush()
        raise ValueError("Device code has expired")

    if device_code.code != normalized_code:
        raise ValueError("Invalid or mismatched device code")

    # Upsert the user record, passing machine_id for abuse carry-over
    from app.services.trial_abuse import get_or_create_user_by_github, detect_trial_abuse_signals

    user = await get_or_create_user_by_github(
        github_login=github_login or "",
        clerk_id=clerk_user_id,
        email=email,
        display_name=display_name,
        session=session,
        machine_id=device_code.machine_id,
        device_id=device_code.device_id,
    )

    # Run abuse detection (async, non-blocking — signals are logged as WARNING)
    try:
        await detect_trial_abuse_signals(
            user=user,
            machine_id=device_code.machine_id,
            session=session,
        )
    except Exception:
        pass  # detection failure must never block auth

    # Mark code as approved
    device_code.status = "approved"
    device_code.clerk_user_id = clerk_user_id
    device_code.user_id = user.id
    device_code.approved_at = datetime.now(tz=timezone.utc)
    await session.flush()

    # Cache the JWT in Redis so the CLI poller gets it fast
    if redis is not None:
        token = issue_jwt(user)
        redis_key = f"device_token:{device_id}"
        await redis.setex(redis_key, DEVICE_CODE_TTL_SECONDS, token)

    return device_code, user


async def poll_status(
    device_id: str,
    session: AsyncSession,
    redis=None,
) -> dict[str, Any]:
    """
    Check the current state of a device code (called by CLI polling).

    Returns:
        { status: "pending" | "approved" | "expired", token?: str }
    """
    # Check Redis cache first (fast path for approved state)
    if redis is not None:
        cached_token = await redis.get(f"device_token:{device_id}")
        if cached_token:
            return {"status": "approved", "token": cached_token}

    result = await session.execute(
        select(DeviceCode).where(DeviceCode.device_id == device_id)
    )
    device_code = result.scalar_one_or_none()

    if device_code is None:
        return {"status": "expired"}

    if device_code.status == "expired":
        return {"status": "expired"}

    if datetime.now(tz=timezone.utc) > device_code.expires_at:
        device_code.status = "expired"
        await session.flush()
        return {"status": "expired"}

    if device_code.status == "approved" and device_code.user_id:
        # Fetch user and issue JWT
        result2 = await session.execute(
            select(User).where(User.id == device_code.user_id)
        )
        user = result2.scalar_one_or_none()
        if user:
            from app.services.trial_abuse import remaining_trial_days  # noqa: PLC0415
            remaining = remaining_trial_days(user)
            # Compute trial_ends_at as today + remaining days (free accounts only)
            trial_ends_at: str | None = None
            if user.plan not in ("pro", "enterprise"):
                from datetime import timedelta  # noqa: PLC0415
                trial_end_date = datetime.now(tz=timezone.utc).date() + timedelta(days=remaining)
                trial_ends_at = trial_end_date.isoformat()
            return {
                "status": "approved",
                "token": issue_jwt(user),
                "user_id": user.id,
                "plan": user.plan,
                "trial_days_remaining": remaining if user.plan not in ("pro", "enterprise") else None,
                "trial_ends_at": trial_ends_at,
            }

    return {"status": "pending"}


def issue_jwt(user: User) -> str:
    """Issue a HS256 JWT with 90-day expiry for a Pakalon user."""
    settings = get_settings()
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": user.id,
        "github": user.github_login,
        "plan": user.plan,
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expire_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
