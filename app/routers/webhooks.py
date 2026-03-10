"""Webhooks router — Polar webhooks (T147)."""
import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.services import billing as billing_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_polar_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Verify the Polar webhook signature using svix Standard Webhooks."""
    if not signature_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook signature",
        )
    settings = get_settings()
    try:
        from svix.webhooks import Webhook  # noqa: PLC0415

        wh = Webhook(settings.polar_webhook_secret)
        # svix expects a dict of headers
        headers = {"webhook-signature": signature_header}
        wh.verify(raw_body, headers)
    except Exception as exc:
        logger.warning("Polar webhook signature verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        ) from exc


@router.post(
    "/polar",
    status_code=status.HTTP_200_OK,
    summary="Polar payment webhook receiver",
)
async def polar_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    webhook_signature: str | None = Header(default=None, alias="webhook-signature"),
):
    """
    Receive and process Polar subscription lifecycle webhooks.

    Events handled:
    - subscription.activated → upgrade user to pro
    - subscription.revoked   → start grace period
    - subscription.updated   → sync current_period_end
    """
    raw_body = await request.body()
    _verify_polar_signature(raw_body, webhook_signature)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        ) from exc

    event_type: str = payload.get("type", "")

    # Idempotency: store processed event IDs to avoid double-processing
    event_id: str = payload.get("event_id") or payload.get("id", "")
    if event_id:
        from app.models.telemetry_event import TelemetryEvent  # noqa: PLC0415
        from sqlalchemy import select  # noqa: PLC0415
        dup = await session.execute(
            select(TelemetryEvent).where(
                TelemetryEvent.event_name == f"webhook:polar:{event_id}",
            )
        )
        if dup.scalar_one_or_none() is not None:
            logger.info("Polar webhook event %s already processed — skipping", event_id)
            return {"received": True, "duplicate": True}
        # Record as processed
        import uuid as _uuid  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        sentinel = TelemetryEvent(
            id=str(_uuid.uuid4()),
            user_id=None,
            event_name=f"webhook:polar:{event_id}",
            properties={"event_type": event_type},
            created_at=datetime.now(tz=timezone.utc),
        )
        session.add(sentinel)

    if event_type == "subscription.activated":
        await billing_svc.handle_polar_subscription_activated(payload, session)
    elif event_type == "subscription.revoked":
        await billing_svc.handle_polar_subscription_revoked(payload, session)
    elif event_type == "subscription.updated":
        # Re-use activated handler to sync updated period
        await billing_svc.handle_polar_subscription_activated(payload, session)
    elif event_type == "subscription.paused":
        await billing_svc.handle_polar_subscription_paused(payload, session)
    elif event_type == "subscription.resumed":
        await billing_svc.handle_polar_subscription_resumed(payload, session)
    elif event_type in ("order.refunded", "order.disputed"):
        await billing_svc.handle_polar_order_refunded_or_disputed(payload, session)
    else:
        logger.info("Unhandled Polar webhook event: %s", event_type)

    await session.commit()
    return {"received": True}


# ──────────────────────────────────────────────────────────────────────────────
# T-BE-09E — Supabase user lifecycle webhooks
# Supabase calls this endpoint on user.created and user.updated events.
# Signature: Authorization: Bearer <SUPABASE_WEBHOOK_SECRET>
# ──────────────────────────────────────────────────────────────────────────────

def _verify_supabase_webhook_secret(authorization: str | None) -> None:
    """Validate the shared-secret Bearer token sent by Supabase webhooks."""
    settings = get_settings()
    expected = settings.supabase_webhook_secret
    if not expected:
        # No secret configured — skip verification (dev/test only)
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook authorization",
        )
    token = authorization.removeprefix("Bearer ").strip()
    import hmac as _hmac  # noqa: PLC0415
    if not _hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


@router.post(
    "/supabase-auth",
    status_code=status.HTTP_200_OK,
    summary="Supabase auth user.created / user.updated webhook",
)
async def supabase_auth_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    """
    Receive Supabase Auth webhook events and sync user profile data to PostgreSQL.

    Handled event types (``type`` field in payload):
    - ``INSERT`` on ``auth.users`` table → create or update user row
    - ``UPDATE`` on ``auth.users`` table → sync email / display_name / metadata
    """
    _verify_supabase_webhook_secret(authorization)

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        ) from exc

    event_type: str = payload.get("type", "")         # "INSERT" | "UPDATE"
    record: dict = payload.get("record") or {}

    supabase_uid: str = record.get("id", "")
    if not supabase_uid:
        # Malformed payload — return 200 to prevent Supabase retry storm
        logger.warning("Supabase auth webhook: no record.id in payload")
        return {"received": True, "skipped": "no_record_id"}

    raw_user_meta: dict = record.get("raw_user_meta_data") or {}
    github_login: str | None = (
        raw_user_meta.get("user_name")
        or raw_user_meta.get("preferred_username")
        or raw_user_meta.get("login")
    )
    email: str | None = record.get("email") or raw_user_meta.get("email")
    display_name: str | None = (
        raw_user_meta.get("full_name")
        or raw_user_meta.get("name")
        or github_login
    )

    if event_type in ("INSERT", "UPDATE"):
        from sqlalchemy import select as _select  # noqa: PLC0415
        from app.models.user import User as UserModel  # noqa: PLC0415
        import uuid as _uuid  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415

        result = await session.execute(
            _select(UserModel).where(UserModel.supabase_id == supabase_uid)
        )
        user = result.scalar_one_or_none()

        if user is None:
            # New Supabase user — create a corresponding Pakalon user row
            user = UserModel(
                id=str(_uuid.uuid4()),
                supabase_id=supabase_uid,
                github_login=github_login or "",
                email=email or "",
                display_name=display_name or github_login or "",
                plan="free",
                created_at=datetime.now(tz=timezone.utc),
                account_deleted=False,
            )
            session.add(user)
            logger.info("Supabase webhook: created user for supabase_id=%s", supabase_uid)
        else:
            # Existing user — sync mutable profile fields
            if github_login:
                user.github_login = github_login
            if email:
                user.email = email
            if display_name:
                user.display_name = display_name
            logger.info("Supabase webhook: updated user for supabase_id=%s", supabase_uid)

        await session.flush()
        await session.commit()
    else:
        logger.info("Supabase auth webhook: unhandled event type %s", event_type)

    return {"received": True}
