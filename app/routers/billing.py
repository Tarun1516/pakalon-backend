"""Billing router — Polar checkout + subscription management (T146)."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.dependencies import get_current_user, require_pro_plan
from app.models.user import User
from app.schemas.billing import CheckoutRequest, CheckoutResponse, SubscriptionStatusResponse, PortalUrlResponse
from app.services import billing as billing_svc
from app.services.email import send_reactivation_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Polar checkout session",
)
async def create_checkout(
    body: CheckoutRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Returns a Polar hosted checkout URL.

    The frontend redirects the user to this URL to complete payment.
    """
    settings = get_settings()
    success_url = body.success_url or f"{settings.frontend_url}/billing/success"

    try:
        url = await billing_svc.create_checkout_url(current_user, success_url)
    except Exception as exc:
        logger.exception("Polar checkout error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not create checkout session",
        ) from exc

    return CheckoutResponse(checkout_url=url)


@router.delete(
    "/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel active subscription",
    dependencies=[Depends(require_pro_plan)],
)
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Cancel the authenticated user's active Pro subscription."""
    cancelled = await billing_svc.cancel_subscription(current_user, session)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active subscription found",
        )
    await session.commit()


@router.get(
    "/subscription",
    response_model=SubscriptionStatusResponse,
    summary="Get current subscription status",
)
async def get_subscription(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return the user's current subscription details."""
    data = await billing_svc.get_subscription_status(current_user.id, session)
    return SubscriptionStatusResponse(**data)


@router.get(
    "/portal-url",
    response_model=PortalUrlResponse,
    summary="Get Polar customer portal URL (update payment method)",
)
async def get_portal_url(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Returns a single-use Polar customer portal URL.

    The user can open this URL in a browser to update their credit card,
    view invoices, or manage their subscription directly in Polar's hosted portal.
    """
    try:
        url = await billing_svc.create_portal_url(current_user, session)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Polar portal URL error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not create portal session",
        ) from exc
    return PortalUrlResponse(portal_url=url)


@router.post(
    "/webhook",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Polar webhook receiver",
    include_in_schema=False,
)
async def polar_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Receive Polar billing webhooks.
    Handles:
    - subscription.active → upgrade user to pro + send reactivation email
    - subscription.revoked / subscription.canceled → downgrade to free
    """
    from sqlalchemy import select  # noqa: PLC0415
    from app.models.user import User as UserModel  # noqa: PLC0415
    from app.models.subscription import Subscription  # noqa: PLC0415

    try:
        payload = await request.json()
    except Exception:
        return

    event_type = payload.get("type", "")
    data = payload.get("data", {})
    polar_sub_id = data.get("id") or data.get("subscription_id")
    customer_email = data.get("customer", {}).get("email") or data.get("customer_email") or ""
    customer_name = data.get("customer", {}).get("name") or data.get("customer_name") or "there"

    if event_type in ("subscription.active", "subscription.updated"):
        # Activate / reactivate user plan
        result = await session.execute(
            select(UserModel).where(UserModel.email == customer_email)
        )
        user = result.scalar_one_or_none()
        if user:
            was_free = user.plan != "pro"
            user.plan = "pro"
            # Upsert subscription record
            sub_result = await session.execute(
                select(Subscription).where(Subscription.polar_id == polar_sub_id)
            )
            sub = sub_result.scalar_one_or_none()
            if sub:
                sub.status = "active"
            await session.commit()
            if was_free and customer_email:
                display = user.display_name or user.github_login or customer_name
                await send_reactivation_email(customer_email, display)
                logger.info("[billing] Reactivation email sent to %s", customer_email)

    elif event_type in ("subscription.revoked", "subscription.canceled"):
        result = await session.execute(
            select(UserModel).where(UserModel.email == customer_email)
        )
        user = result.scalar_one_or_none()
        if user:
            user.plan = "free"
            await session.commit()
            logger.info("[billing] Downgraded %s to free (event: %s)", customer_email, event_type)
