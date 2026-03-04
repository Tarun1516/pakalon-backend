"""Auth router — device code flow (T029)."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_supabase_user
from app.schemas.auth import (
    DeviceCodeCreateRequest,
    DeviceCodeCreateResponse,
    DeviceCodeConfirmRequest,
    DeviceCodeConfirmResponse,
    DeviceCodePollResponse,
    DeviceCodeWebConfirmRequest,
    DeviceCodeWebConfirmResponse,
    WebSignInRequest,
    WebSignInResponse,
)
from app.services import device_code as device_code_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _get_redis():
    """Lazily import redis singleton to avoid circular imports at module load."""
    from app.main import redis_client  # noqa: PLC0415
    return redis_client


@router.post(
    "/devices",
    response_model=DeviceCodeCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate device code auth — CLI step 1",
)
async def create_device_code(
    body: DeviceCodeCreateRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    CLI calls this to start the authentication flow.

    Returns a 6-digit code + a device_id the CLI should keep for polling.
    """
    device_id = body.device_id or str(uuid.uuid4())
    try:
        dc = await device_code_svc.create_device_code(
            device_id=device_id,
            machine_id=body.machine_id,
            session=session,
            redis=_get_redis(),
        )
        await session.commit()
    except Exception as exc:
        logger.exception("Error creating device code: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create device code",
        ) from exc

    return DeviceCodeCreateResponse(
        device_id=device_id,
        code=dc.code,
        expires_in=device_code_svc.DEVICE_CODE_TTL_SECONDS,
    )


@router.get(
    "/devices/{device_id}/token",
    response_model=DeviceCodePollResponse,
    summary="Poll for token — CLI step 2 (long-poll)",
)
async def poll_device_token(
    device_id: str,
    session: AsyncSession = Depends(get_session),
):
    """
    CLI polls this until status == 'approved'.

    - 200 + token: auth completed, JWT in body
    - 202: still pending, keep polling
    - 410: code expired / not found
    """
    result = await device_code_svc.poll_status(
        device_id=device_id,
        session=session,
        redis=_get_redis(),
    )
    await session.commit()

    if result["status"] == "approved":
        return DeviceCodePollResponse(
            status="approved",
            token=result.get("token"),
            user_id=result.get("user_id"),
            plan=result.get("plan"),
            trial_days_remaining=result.get("trial_days_remaining"),
            trial_ends_at=result.get("trial_ends_at"),
        )

    if result["status"] == "expired":
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Device code expired",
        )

    # pending
    return DeviceCodePollResponse(status="pending")


@router.post(
    "/devices/{device_id}/confirm",
    response_model=DeviceCodeConfirmResponse,
    summary="Confirm code from website — web step 3",
)
async def confirm_device_code(
    device_id: str,
    body: DeviceCodeConfirmRequest,
    session: AsyncSession = Depends(get_session),
    supabase_payload: dict = Depends(get_supabase_user),
):
    """
    The Pakalon website calls this after the user logs in with Supabase GitHub OAuth
    and enters (or auto-submits) the 6-digit code shown in the CLI.

    Requires a valid Supabase JWT in the Authorization header.
    """
    supabase_user_id: str = supabase_payload["sub"]

    # Extract GitHub identity from Supabase user_metadata (populated by GitHub OAuth)
    user_meta = supabase_payload.get("user_metadata", {})
    github_login: str | None = (
        user_meta.get("user_name")
        or user_meta.get("preferred_username")
        or user_meta.get("login")
    )
    email: str | None = supabase_payload.get("email") or user_meta.get("email")
    display_name: str | None = (
        user_meta.get("full_name")
        or user_meta.get("name")
        or github_login
    )

    try:
        dc, user = await device_code_svc.confirm_code(
            device_id=device_id,
            code=body.code,
            clerk_user_id=supabase_user_id,
            github_login=github_login,
            email=email,
            display_name=display_name,
            session=session,
            redis=_get_redis(),
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    token = device_code_svc.issue_jwt(user)
    return DeviceCodeConfirmResponse(
        status="approved",
        token=token,
        user_id=user.id,
        plan=user.plan,
    )


@router.post(
    "/devices/{device_id}/web-confirm",
    response_model=DeviceCodeWebConfirmResponse,
    summary="Confirm device code from web UI — no JWT required",
)
async def web_confirm_device_code(
    device_id: str,
    body: DeviceCodeWebConfirmRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Called by the web `/[device_id]/auth/` page after the user enters the
    6-digit code.  No authentication required — a user record is
    created or looked up by email / github_login.

    The CLI polls `/devices/{device_id}/token` concurrently and will receive
    the JWT as soon as this endpoint writes it to Redis.
    """
    try:
        _dc, user, token = await device_code_svc.web_confirm_code(
            device_id=device_id,
            code=body.code,
            email=body.email,
            github_login=body.github_login,
            display_name=body.display_name,
            session=session,
            redis=_get_redis(),
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Error in web_confirm_device_code: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication failed due to a server error",
        ) from exc

    return DeviceCodeWebConfirmResponse(
        status="approved",
        user_id=user.id,
        plan=user.plan,
        token=token,
        message=(
            "Authentication successful! "
            "You may close this window and start building applications using Pakalon."
        ),
    )


@router.post(
    "/web-signin",
    response_model=WebSignInResponse,
    status_code=status.HTTP_200_OK,
    summary="Exchange Supabase GitHub OAuth session for a Pakalon JWT",
)
async def web_signin(
    body: WebSignInRequest,
    supabase_payload: dict = Depends(get_supabase_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Called by the web dashboard login page after Supabase GitHub OAuth completes.

    Accepts the Supabase access token (via Authorization: Bearer) plus the user's
    GitHub login extracted from the Supabase session on the frontend.  Creates or
    finds the user record and returns a Pakalon JWT for subsequent API calls.
    """
    from app.services.trial_abuse import get_or_create_user_by_github  # noqa: PLC0415

    # Supabase user UUID — stored in the clerk_id column (external auth provider ID)
    supabase_user_id: str = supabase_payload.get("sub", "")
    if not supabase_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Supabase token missing subject claim",
        )

    try:
        user = await get_or_create_user_by_github(
            github_login=body.github_login,
            clerk_id=supabase_user_id,
            email=body.email,
            display_name=body.display_name,
            session=session,
        )
        await session.commit()
    except Exception as exc:
        logger.exception("Error in web_signin: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Sign-in failed due to a server error",
        ) from exc

    token = device_code_svc.issue_jwt(user)
    return WebSignInResponse(
        token=token,
        user_id=user.id,
        plan=user.plan,
        github_login=user.github_login,
    )
