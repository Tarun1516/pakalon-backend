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
