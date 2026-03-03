"""Auth router — device code flow (T029)."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_clerk_user
from app.schemas.auth import (
    DeviceCodeCreateRequest,
    DeviceCodeCreateResponse,
    DeviceCodeConfirmRequest,
    DeviceCodeConfirmResponse,
    DeviceCodePollResponse,
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
    clerk_payload: dict = Depends(get_clerk_user),
):
    """
    The Pakalon website calls this after the user logs in with Clerk and enters
    (or auto-submits) the 6-digit code shown in the CLI.

    Requires a valid Clerk JWT in the Authorization header.
    """
    clerk_user_id: str = clerk_payload["sub"]

    # T-BACK-15: Extract GitHub identity from the verified external_accounts claim.
    # _enforce_github_provider() in get_clerk_user already confirmed the provider is GitHub.
    _ext = clerk_payload.get("external_accounts") or []
    _github_account = next(
        (
            a for a in _ext
            if "github" in str(a.get("provider", "")).lower()
        ),
        _ext[0] if _ext else {},
    )
    github_login: str | None = (
        _github_account.get("username")
        or _github_account.get("external_id")
        or clerk_payload.get("username")
    )
    email: str | None = (clerk_payload.get("email_addresses") or [{}])[0].get("email_address")
    display_name: str | None = (
        clerk_payload.get("first_name") or ""
    ) + " " + (clerk_payload.get("last_name") or "")
    display_name = display_name.strip() or None

    try:
        dc, user = await device_code_svc.confirm_code(
            device_id=device_id,
            code=body.code,
            clerk_user_id=clerk_user_id,
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
