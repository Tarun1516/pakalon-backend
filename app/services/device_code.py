"""Device code service — core 6-character auth flow."""
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.device_code import DeviceCode
from app.models.user import User

logger = logging.getLogger(__name__)

DEVICE_CODE_TTL_SECONDS = 600  # 10 minutes
DEVICE_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


async def _build_approved_poll_payload(
    user: User,
    token: str,
    session: AsyncSession,
) -> dict[str, Any]:
    """Return the full CLI poll payload for an approved device code."""
    from app.services.trial_abuse import remaining_trial_days  # noqa: PLC0415
    from app.services.billing import get_subscription_status  # noqa: PLC0415

    remaining = remaining_trial_days(user)
    trial_ends_at: str | None = None
    billing_days_remaining: int | None = None
    if user.plan not in ("pro", "enterprise"):
        trial_end_date = utcnow().date() + timedelta(days=remaining)
        trial_ends_at = trial_end_date.isoformat()
    else:
        billing_state = await get_subscription_status(user.id, session)
        billing_days_remaining = billing_state.get("days_remaining")

    return {
        "status": "approved",
        "token": token,
        "user_id": user.id,
        "plan": user.plan,
        "github_login": user.github_login,
        "display_name": user.display_name,
        "trial_days_remaining": remaining if user.plan not in ("pro", "enterprise") else None,
        "billing_days_remaining": billing_days_remaining,
        "trial_ends_at": trial_ends_at,
    }


async def _encode_approved_poll_payload(
    user: User,
    token: str,
    session: AsyncSession,
) -> str:
    """Serialize the approved poll payload for Redis caching."""
    return json.dumps(await _build_approved_poll_payload(user, token, session))


def _decode_cached_poll_payload(value: Any) -> dict[str, Any] | None:
    """Decode a cached Redis poll payload, supporting legacy token-only entries."""
    if value is None:
        return None

    if isinstance(value, bytes):
        value = value.decode("utf-8")

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"status": "approved", "token": value}
        if isinstance(parsed, dict):
            parsed.setdefault("status", "approved")
            return parsed
        return None

    if isinstance(value, dict):
        value.setdefault("status", "approved")
        return value

    return None


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return ensure_utc(datetime.now(tz=timezone.utc))


def ensure_utc(value: datetime) -> datetime:
    """Normalize DB datetimes so SQLite tests and Postgres behave consistently."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def generate_code() -> str:
    """Generate a cryptographically random 6-character alphanumeric code."""
    return "".join(secrets.choice(DEVICE_CODE_ALPHABET) for _ in range(6))


def normalize_code(code: str | None) -> str:
    """Normalize user-entered auth codes before validation."""
    return (code or "").strip().upper()


def is_valid_code_format(code: str) -> bool:
    """Validate that *code* is a 6-character Pakalon auth code."""
    return len(code) == 6 and all(ch in DEVICE_CODE_ALPHABET for ch in code)


async def detect_launch_experience(
    device_id: str,
    machine_id: str | None,
    session: AsyncSession,
) -> tuple[bool, str]:
    """
    Decide whether the CLI should show the first-run video or returning-run text logo.

    A machine is considered "returning" if we have previously seen its machine_id,
    device_id, session activity, or login activity.
    """
    from app.models.login_event import LoginEvent  # noqa: PLC0415
    from app.models.machine_id import MachineId  # noqa: PLC0415
    from app.models.session import Session  # noqa: PLC0415

    try:
        if machine_id:
            checks = [
                select(MachineId.id).where(MachineId.machine_id == machine_id).limit(1),
                select(LoginEvent.id).where(LoginEvent.machine_id == machine_id).limit(1),
                select(Session.id).where(Session.machine_id == machine_id).limit(1),
                select(DeviceCode.id).where(DeviceCode.machine_id == machine_id).limit(1),
            ]
            for query in checks:
                existing = await session.execute(query)
                if existing.scalar_one_or_none() is not None:
                    return False, "text"

        if device_id:
            existing_device = await session.execute(
                select(DeviceCode.id).where(DeviceCode.device_id == device_id).limit(1)
            )
            if existing_device.scalar_one_or_none() is not None:
                return False, "text"
    except SQLAlchemyError:
        await session.rollback()
        logger.debug("Falling back to first-run launch experience detection", exc_info=True)
        return True, "video"

    return True, "video"


async def create_device_code(
    device_id: str,
    machine_id: str | None,
    session: AsyncSession,
    redis=None,
) -> tuple[DeviceCode, bool, str]:
    """
    Create a new device code record in PostgreSQL and store in Redis with TTL.

    Returns the newly created DeviceCode plus launch metadata.
    """
    is_first_machine_run, launch_experience = await detect_launch_experience(
        device_id=device_id,
        machine_id=machine_id,
        session=session,
    )

    # Replace any existing code row for this device_id so re-auth works cleanly.
    existing = await session.execute(
        select(DeviceCode).where(DeviceCode.device_id == device_id)
    )
    existing_code = existing.scalar_one_or_none()
    if existing_code:
        await session.delete(existing_code)
        await session.flush()

    code = generate_code()
    expires_at = utcnow() + timedelta(seconds=DEVICE_CODE_TTL_SECONDS)

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

    # Cache in Redis for fast polling (best-effort; not required)
    if redis is not None:
        try:
            redis_key = f"device_code:{device_id}"
            await redis.setex(redis_key, DEVICE_CODE_TTL_SECONDS, code)
        except Exception:
            logger.warning("Redis unavailable — device code created in DB only")

    return device_code, is_first_machine_run, launch_experience


async def confirm_code(
    device_id: str,
    code: str,
    supabase_user_id: str,
    github_login: str | None,
    email: str | None,
    display_name: str | None,
    session: AsyncSession,
    redis=None,
) -> tuple[DeviceCode, User]:
    """
    Confirm a device code from the website (user has authenticated via Supabase).

    Returns (device_code, user) on success.
    Raises ValueError for invalid/expired codes.
    """
    # Enforce strict 6-character Pakalon code format server-side
    normalized_code = normalize_code(code)
    if not is_valid_code_format(normalized_code):
        raise ValueError("Invalid code format. Code must be 6 letters/numbers")

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

    if utcnow() > ensure_utc(device_code.expires_at):
        device_code.status = "expired"
        await session.flush()
        raise ValueError("Device code has expired")

    if device_code.code != normalized_code:
        raise ValueError("Invalid or mismatched device code")

    # Upsert the user record, passing machine_id for abuse carry-over
    from app.services.trial_abuse import get_or_create_user_by_github, detect_trial_abuse_signals

    user = await get_or_create_user_by_github(
        github_login=github_login or "",
        supabase_id=supabase_user_id,
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
    device_code.supabase_user_id = supabase_user_id
    device_code.user_id = user.id
    device_code.approved_at = utcnow()
    await session.flush()

    # Cache the JWT in Redis so the CLI poller gets it fast (best-effort)
    if redis is not None:
        token = issue_jwt(user)
        try:
            redis_key = f"device_token:{device_id}"
            await redis.setex(
                redis_key,
                DEVICE_CODE_TTL_SECONDS,
                await _encode_approved_poll_payload(user, token, session),
            )
        except Exception:
            logger.warning("Redis unavailable — JWT not cached, CLI will poll DB")

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
        try:
            cached_payload = _decode_cached_poll_payload(await redis.get(f"device_token:{device_id}"))
            if cached_payload and cached_payload.get("token") and cached_payload.get("user_id"):
                return cached_payload
        except Exception:
            logger.warning("Redis unavailable — falling through to DB poll")

    result = await session.execute(
        select(DeviceCode).where(DeviceCode.device_id == device_id)
    )
    device_code = result.scalar_one_or_none()

    if device_code is None:
        return {"status": "expired"}

    if device_code.status == "expired":
        return {"status": "expired"}

    if utcnow() > ensure_utc(device_code.expires_at):
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
            return await _build_approved_poll_payload(user, issue_jwt(user), session)

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


async def web_confirm_code(
    device_id: str,
    code: str,
    email: str | None,
    github_login: str | None,
    display_name: str | None,
    session: AsyncSession,
    redis=None,
) -> tuple[DeviceCode, User, str]:
    """
    Confirm a device code submitted from the web UI (no Clerk JWT required).

    Creates or finds a user based on email / github_login, marks the device
    code as approved, and caches the JWT in Redis so the CLI poller gets it.

    Returns (device_code, user) on success.
    Raises ValueError for invalid/expired/unknown codes.
    """
    normalized_code = normalize_code(code)
    if not is_valid_code_format(normalized_code):
        raise ValueError("Invalid code format. Code must be 6 letters/numbers")

    result = await session.execute(
        select(DeviceCode).where(
            DeviceCode.device_id == device_id,
            DeviceCode.status == "pending",
        )
    )
    device_code = result.scalar_one_or_none()

    if device_code is None:
        raise ValueError("Device code not found or already used")

    if utcnow() > ensure_utc(device_code.expires_at):
        device_code.status = "expired"
        await session.flush()
        raise ValueError("Device code has expired")

    if device_code.code != normalized_code:
        raise ValueError("Invalid device code")

    # Derive stable identifiers from whatever the web page provides.
    # Fall back to a deterministic slug so user creation never fails.
    _github_login = (
        github_login
        or (email.split("@")[0] if email else None)
        or f"device_{device_code.device_id[:8]}"
    )
    # Use a synthetic supabase_id so the upsert path in get_or_create_user_by_github
    # stays stable across re-auths from the same device.
    _supabase_id = f"web_{device_code.device_id}"

    from app.services.trial_abuse import get_or_create_user_by_github  # noqa: PLC0415

    user = await get_or_create_user_by_github(
        github_login=_github_login,
        supabase_id=_supabase_id,
        email=email,
        display_name=display_name,
        session=session,
        machine_id=device_code.machine_id,
        device_id=device_code.device_id,
    )

    # Mark device code as approved
    device_code.status = "approved"
    device_code.supabase_user_id = _supabase_id
    device_code.user_id = user.id
    device_code.approved_at = utcnow()
    await session.flush()

    # Issue JWT for both the CLI poller and the web dashboard session
    token = issue_jwt(user)

    # Cache the JWT in Redis so the CLI poller picks it up immediately (best-effort)
    if redis is not None:
        try:
            redis_key = f"device_token:{device_id}"
            await redis.setex(
                redis_key,
                DEVICE_CODE_TTL_SECONDS,
                await _encode_approved_poll_payload(user, token, session),
            )
        except Exception:
            logger.warning("Redis unavailable — JWT not cached for web_confirm_code")

    return device_code, user, token
