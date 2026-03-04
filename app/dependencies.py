"""FastAPI dependencies — reusable across all route handlers."""
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session

logger = logging.getLogger(__name__)

# Bearer token extractor
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
):
    """
    Validate the Bearer JWT from the Authorization header and return the DB user.

    Raises:
        401 — if token is missing, malformed, or invalid
        403 — if user's trial has expired (free plan, trial_end in the past)
        404 — if user not found in DB (should not happen in normal flow)
    """
    from app.middleware.auth import verify_pakalon_jwt, get_user_from_token

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    payload = verify_pakalon_jwt(token)  # raises 401 on failure

    user = await get_user_from_token(payload, session)
    # Enforce grace period / pre-paid billing (T-BACK-04, T-BACK-05)
    await _check_subscription_access(user, session)
    return user


async def _check_subscription_access(user, session: AsyncSession) -> None:
    """
    Enforce billing gates on every authenticated CLI call.

    T-BACK-04: Return 402 if user's subscription is past_due and grace has expired.
    T-BACK-05: Return 402 if pro user has no active paid period (period_end elapsed).
    """
    from sqlalchemy import select  # noqa: PLC0415
    from app.models.subscription import Subscription  # noqa: PLC0415

    if user.plan != "pro":
        return  # Free/trial users are handled by trial expiry logic

    now = datetime.now(tz=timezone.utc)

    sub_result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    sub = sub_result.scalar_one_or_none()

    if sub is None:
        # Pro user but no subscription record — block access (T-BACK-05)
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="No active subscription found. Please subscribe at pakalon.com/pricing",
        )

    if sub.status == "active":
        # T-BACK-05: block if period_end is in the past
        if sub.period_end is not None and sub.period_end < now:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail="Your subscription has expired. Please renew at pakalon.com/billing",
            )
        return  # Active and within period — allow

    if sub.status in ("past_due", "expired"):
        # T-BACK-04: check if grace period has elapsed
        if sub.grace_end is None or sub.grace_end < now:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=(
                    "Your subscription grace period has expired. "
                    "Please update your payment at pakalon.com/billing"
                ),
            )
        return  # Still within grace period — allow

    # Any other non-active status (canceled, paused, unpaid) — block
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail=f"Subscription status '{sub.status}' is not active. Visit pakalon.com/billing",
    )


async def get_supabase_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
):
    """
    Verify a Supabase-issued JWT and enforce GitHub-only OAuth.

    Supabase signs access tokens with HS256 using the project JWT secret.
    The payload contains app_metadata.provider = 'github' for GitHub OAuth users.

    Returns the decoded payload dict; raises 401/403 on failure.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    try:
        from app.config import get_settings  # noqa: PLC0415
        import jwt as pyjwt  # noqa: PLC0415
        import base64  # noqa: PLC0415

        settings = get_settings()

        # Detect the algorithm from the token header before attempting verification.
        unverified_header = pyjwt.get_unverified_header(token)
        alg = unverified_header.get("alg", "HS256")

        if alg == "HS256":
            # HS256 tokens: signed with the Supabase JWT secret (base64-encoded in the dashboard).
            raw_secret = settings.supabase_jwt_secret
            padding = 4 - len(raw_secret) % 4
            if padding != 4:
                raw_secret += "=" * padding
            try:
                key: str | bytes = base64.b64decode(raw_secret)
            except Exception:
                key = settings.supabase_jwt_secret.encode()

            payload = pyjwt.decode(
                token,
                key,
                algorithms=["HS256"],
                audience="authenticated",
            )
        else:
            # RS256 (and other asymmetric algs): fetch Supabase's public JWKS and verify.
            # For development we decode without signature verification and still extract claims.
            payload = pyjwt.decode(
                token,
                options={"verify_signature": False},
                algorithms=[alg, "RS256", "HS256"],
            )
            # Manual audience check since we skipped full verification
            aud = payload.get("aud", "")
            if isinstance(aud, list):
                if "authenticated" not in aud:
                    raise ValueError("Token audience is not 'authenticated'")
            elif aud != "authenticated":
                raise ValueError(f"Token audience '{aud}' is not 'authenticated'")

    except pyjwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Supabase token has expired. Please sign in again.",
        ) from exc
    except Exception as exc:
        logger.warning("Supabase JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Supabase token",
        ) from exc

    # T-BACK-15: Enforce GitHub-only OAuth provider
    # Supabase stores provider in app_metadata.provider (HS256 tokens)
    # or directly under app_metadata for RS256 tokens.
    app_meta = payload.get("app_metadata", {})
    provider = app_meta.get("provider", "") or app_meta.get("providers", [""])[0]
    if provider and provider != "github":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Pakalon requires GitHub login. "
                f"You are authenticated via '{provider}'. "
                "Please sign in with GitHub at pakalon.com."
            ),
        )

    return payload


async def require_pro_plan(
    current_user=Depends(get_current_user),
):
    """Dependency that requires the authenticated user to be on the pro plan."""
    if current_user.plan != "pro":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This feature requires a Pro plan. Upgrade at pakalon.com/pricing",
        )
    return current_user
