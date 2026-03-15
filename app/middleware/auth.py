"""Pakalon JWT authentication middleware and helpers."""
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import jwt
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger(__name__)


def _get_redis():
    """Lazily resolve the shared Redis client to avoid import cycles."""
    from app.main import redis_client  # noqa: PLC0415

    return redis_client


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ensure_utc(value: datetime) -> datetime:
    """Normalize DB datetimes so SQLite tests and Postgres behave consistently."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def verify_pakalon_jwt(token: str) -> dict[str, Any]:
    """
    Verify a Pakalon-issued JWT (HS256, 90-day expiry).

    Returns the decoded payload on success.
    Raises 401 HTTPException on failure.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["sub", "exp", "iat"]},
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired. Run `pakalon` to re-authenticate.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        logger.debug("Invalid JWT: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def is_token_revoked(token: str) -> bool:
    """Check whether a JWT has been explicitly revoked (logout)."""
    redis = _get_redis()
    if redis is None:
        return False
    try:
        key = f"revoked_token:{_token_fingerprint(token)}"
        return bool(await redis.get(key))
    except Exception:
        # Redis outages should not hard-block API auth checks.
        return False


async def revoke_token(token: str, exp_claim: int | float | None) -> bool:
    """Mark a JWT as revoked in Redis until it would naturally expire."""
    redis = _get_redis()
    if redis is None:
        return False

    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    exp_ts = int(exp_claim) if exp_claim is not None else (now_ts + 86_400)
    ttl_seconds = max(60, exp_ts - now_ts)

    try:
        key = f"revoked_token:{_token_fingerprint(token)}"
        await redis.setex(key, ttl_seconds, "1")
        return True
    except Exception:
        return False


async def get_user_from_token(payload: dict[str, Any], session: AsyncSession):
    """
    Look up the user record for the JWT subject.

    Raises:
        401 — user not found in DB
        403 — account deleted
        403 — pro subscription grace period expired (past grace_end)
    """
    from app.models.user import User  # local import to avoid circular
    from app.models.subscription import Subscription  # local import

    user_id: str | None = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token: missing subject",
        )

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if user.account_deleted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deleted",
        )

    now = _ensure_utc(datetime.now(tz=timezone.utc))

    # T-BACK-06: Grace period enforcement for pro users
    # If a pro user's subscription has expired, check grace_end.
    # Within grace period → allow (plan stays 'pro' in JWT but subscription status is checked)
    # Past grace_end → downgrade effective plan to 'free' and block unless has trial remaining
    if user.plan == "pro":
        sub_result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        subscription = sub_result.scalar_one_or_none()

        if subscription is not None:
            # Subscription is revoked/canceled and past grace period
            if subscription.status in ("canceled", "revoked") and subscription.grace_end is not None:
                if _ensure_utc(subscription.grace_end) < now:
                    # Grace period expired — effective plan is 'free'
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=(
                            "Your Pro subscription has expired and the grace period has ended. "
                            "Visit pakalon.com/pricing to renew."
                        ),
                    )
                else:
                    # Within grace period — access allowed, but log a warning
                    logger.info(
                        "User %s is in subscription grace period (grace_end=%s)",
                        user_id,
                        subscription.grace_end,
                    )

    return user


async def check_context_window_exhaustion(
    user_id: str,
    model_id: str,
    session: AsyncSession,
) -> None:
    """
    T-BACK-03: Block AI calls when context window is exhausted for the current session.

    Checks the latest context_window_used vs context_window_size for the model.
    Raises HTTP 429 if the context window is at or above 100%.
    """
    from app.models.model_usage import ModelUsage

    result = await session.execute(
        select(ModelUsage)
        .where(
            ModelUsage.user_id == user_id,
            ModelUsage.model_id == model_id,
        )
        .order_by(ModelUsage.created_at.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()

    if latest is not None and latest.context_window_size > 0:
        used_pct = latest.context_window_used / latest.context_window_size
        if used_pct >= 1.0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Context window exhausted for model {model_id}. "
                    "Start a new session or switch to a model with a larger context window."
                ),
                headers={"Retry-After": "0"},
            )
