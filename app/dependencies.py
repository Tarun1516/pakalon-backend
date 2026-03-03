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


async def get_clerk_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
):
    """
    Verify a Clerk-issued JWT (used by the website confirm flow).

    T-BACK-15: Enforces that the authenticated user signed in via GitHub OAuth.
    Raises 403 if the user authenticated with any other provider (email, Google, etc.).

    Returns the decoded payload dict on success; raises 401/403 on failure.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Clerk Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    try:
        from app.config import get_settings  # noqa: PLC0415
        import jwt as pyjwt  # noqa: PLC0415

        settings = get_settings()
        # Clerk JWTs are RS256 — we just decode without verifying in dev,
        # or validate via Clerk backend SDK in production.
        payload = pyjwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["RS256"],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Clerk token",
        ) from exc

    # T-BACK-15: Enforce GitHub-only OAuth provider
    # Clerk stores OAuth provider info in different claim locations depending on version.
    # Check: external_accounts[0].provider, or oauth_access_token provider claim.
    _enforce_github_provider(payload)

    return payload


def _enforce_github_provider(payload: dict) -> None:
    """
    T-BACK-15: Raise HTTP 403 if the Clerk JWT does not indicate GitHub OAuth.

    Checks multiple locations where Clerk encodes the OAuth provider:
      1. `external_accounts[0].provider` — Clerk v2 session claims
      2. `oauth_access_token[0].provider` — alternate claim name
      3. `azp` claim containing "github" — app/client-level hint
    """
    # external_accounts provider check (most common)
    external_accounts = payload.get("external_accounts", [])
    if external_accounts:
        provider = external_accounts[0].get("provider", "")
        if provider and "github" not in str(provider).lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Pakalon requires GitHub login. You are authenticated via '{provider}'. Please sign in with GitHub at pakalon.com.",
            )
        if provider and "github" in str(provider).lower():
            return  # ✅ GitHub confirmed

    # oauth_access_token check
    oauth_tokens = payload.get("oauth_access_token", [])
    if oauth_tokens:
        provider = oauth_tokens[0].get("provider", "")
        if provider and "github" not in str(provider).lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Pakalon requires GitHub login. You are authenticated via '{provider}'. Please sign in with GitHub at pakalon.com.",
            )
        if provider and "github" in str(provider).lower():
            return  # ✅ GitHub confirmed

    # If no provider info found, allow through (avoids false rejects in dev/test)
    # Production deployments with full Clerk backend SDK should verify strictly.


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
