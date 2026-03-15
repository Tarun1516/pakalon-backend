"""Billing service — Polar SDK integration (T145)."""
from collections import defaultdict
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.subscription import Subscription
from app.models.user import User
from app.services.webhook_retry import with_retry, record_dead_letter

logger = logging.getLogger(__name__)

GRACE_PERIOD_DAYS = 3
SECURITY_DEPOSIT_USD = 2.00
PLATFORM_FEE_RATE = 0.10


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _estimate_cycle_usage_costs(
    user_id: str,
    period_start: datetime,
    period_end: datetime,
    session: AsyncSession,
) -> dict[str, Any]:
    """Estimate cycle charges from model_usage + cached OpenRouter model pricing."""
    from app.models.model_cache import ModelCache  # noqa: PLC0415
    from app.models.model_usage import ModelUsage  # noqa: PLC0415

    usage_rows = await session.execute(
        select(ModelUsage.model_id, ModelUsage.tokens_used)
        .where(
            ModelUsage.user_id == user_id,
            ModelUsage.created_at >= period_start,
            ModelUsage.created_at <= period_end,
        )
    )
    usage_by_model: dict[str, int] = defaultdict(int)
    for model_id, tokens_used in usage_rows.all():
        usage_by_model[model_id] += int(tokens_used or 0)

    if not usage_by_model:
        return {
            "cycle_token_usage": 0,
            "usage_charges_usd": 0.0,
            "platform_fee_usd": 0.0,
            "deposit_applied_usd": 0.0,
            "estimated_total_due_usd": 0.0,
            "usage_by_model": [],
        }

    model_rows = await session.execute(
        select(ModelCache.model_id, ModelCache.raw_json)
        .where(ModelCache.model_id.in_(list(usage_by_model.keys())))
    )
    pricing_by_model = {
        row.model_id: (row.raw_json or {}).get("pricing", {})
        for row in model_rows
    }

    usage_breakdown: list[dict[str, Any]] = []
    usage_charges = 0.0
    total_tokens = 0
    for model_id, tokens in usage_by_model.items():
        pricing = pricing_by_model.get(model_id, {})
        prompt_cost = _safe_float(pricing.get("prompt"), 0.0)
        completion_cost = _safe_float(pricing.get("completion"), 0.0)
        effective_unit_cost = max(0.0, (prompt_cost + completion_cost) / 2)
        model_cost = tokens * effective_unit_cost

        total_tokens += tokens
        usage_charges += model_cost
        usage_breakdown.append(
            {
                "model_id": model_id,
                "tokens": tokens,
                "approx_usage_usd": round(model_cost, 6),
            }
        )

    usage_breakdown.sort(key=lambda item: item["tokens"], reverse=True)

    platform_fee = usage_charges * PLATFORM_FEE_RATE
    gross_due = usage_charges + platform_fee
    deposit_applied = min(SECURITY_DEPOSIT_USD, gross_due)
    net_due = max(0.0, gross_due - deposit_applied)

    return {
        "cycle_token_usage": int(total_tokens),
        "usage_charges_usd": round(usage_charges, 6),
        "platform_fee_usd": round(platform_fee, 6),
        "deposit_applied_usd": round(deposit_applied, 6),
        "estimated_total_due_usd": round(net_due, 6),
        "usage_by_model": usage_breakdown,
    }


async def _get_polar_client():
    """Return a configured Polar SDK client."""
    from polar_sdk import Polar  # noqa: PLC0415
    settings = get_settings()
    return Polar(access_token=settings.polar_access_token)


async def create_portal_url(user: User, session: AsyncSession) -> str:
    """
    Create a Polar customer portal URL so the user can manage their subscription
    and update payment details.

    Returns the hosted portal URL (single-use, short-lived session token).
    """
    polar = await _get_polar_client()

    # Find the user's Polar customer_id from their active (or most recent) subscription
    sub_result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    sub = sub_result.scalar_one_or_none()
    polar_customer_id: str | None = getattr(sub, "polar_customer_id", None) if sub else None

    if not polar_customer_id:
        # Fall back: create a portal URL scoped by email if available
        if not user.email:
            raise ValueError("No Polar customer found for this account — subscribe first.")
        # Create a customer session using email lookup
        portal_session = await with_retry(
            lambda: polar.customer_sessions.create(
                request={"customer_email": user.email}
            ),
            service="polar",
            operation="customer_sessions.create",
            payload={"user_id": user.id, "email": user.email},
            session=None,
        )
    else:
        portal_session = await with_retry(
            lambda: polar.customer_sessions.create(
                request={"customer_id": polar_customer_id}
            ),
            service="polar",
            operation="customer_sessions.create",
            payload={"user_id": user.id, "polar_customer_id": polar_customer_id},
            session=None,
        )

    # The Polar SDK returns customer_portal_url on the session object
    return portal_session.customer_portal_url

async def create_checkout_url(user: User, success_url: str) -> str:
    """
    Create a Polar checkout session for the Pro plan.

    Returns the hosted checkout URL.
    """
    polar = await _get_polar_client()
    settings = get_settings()

    _checkout_payload = {
        "product_price_id": settings.polar_product_price_id,
        "user_id": user.id,
        "success_url": success_url,
    }
    checkout = await with_retry(
        lambda: polar.checkouts.create(
            request={
                "product_price_id": settings.polar_product_price_id,
                "success_url": success_url,
                "customer_email": user.email or None,
                "metadata": {"pakalon_user_id": user.id},
            }
        ),
        service="polar",
        operation="checkouts.create",
        payload=_checkout_payload,
        session=None,  # checkout flow has no AsyncSession — dead-letter skipped
    )
    return checkout.url


async def cancel_subscription(user: User, session: AsyncSession) -> bool:
    """
    Cancel the user's active Polar subscription.

    Returns True if cancellation succeeded.
    """
    polar = await _get_polar_client()

    sub_result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status == "active",
        )
    )
    active_sub = sub_result.scalar_one_or_none()
    if active_sub is None:
        return False

    await with_retry(
        lambda: polar.subscriptions.cancel(id=active_sub.polar_sub_id),
        service="polar",
        operation="subscriptions.cancel",
        payload={"polar_sub_id": active_sub.polar_sub_id, "user_id": user.id},
        session=session,
    )
    active_sub.status = "canceled"
    await session.flush()
    return True


async def get_subscription_status(
    user_id: str, session: AsyncSession
) -> dict[str, Any]:
    """Return the current subscription details for a user."""
    result = await session.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    sub = result.scalar_one_or_none()
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()

    if sub is None:
        return {
            "status": "none",
            "plan": user.plan if user else "free",
            "billing_model": "postpaid_usage",
            "security_deposit_usd": SECURITY_DEPOSIT_USD,
            "platform_fee_rate": PLATFORM_FEE_RATE,
            "usage_charges_usd": 0.0,
            "platform_fee_usd": 0.0,
            "deposit_applied_usd": 0.0,
            "estimated_total_due_usd": 0.0,
            "cycle_token_usage": 0,
            "usage_by_model": [],
        }
    now = datetime.now(tz=timezone.utc)
    period_start = _ensure_utc(sub.period_start)
    period_end = _ensure_utc(sub.period_end)
    grace_end = _ensure_utc(sub.grace_end)
    
    # T-BE-08: Calculate days remaining in billing cycle
    days_remaining: int | None = None
    in_grace_period = False
    
    if period_start and period_end:
        cycle_days = (period_end - period_start).days
        if cycle_days > 0:
            days_passed = (now - period_start).days
            days_remaining = max(0, cycle_days - days_passed)
    
    # T-BE-09: Check if in grace period
    if grace_end and now < grace_end:
        in_grace_period = True
        days_remaining = max(days_remaining or 0, (grace_end - now).days)

    period_start_for_estimate = period_start or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    period_end_for_estimate = min(now, period_end) if period_end else now
    cycle_estimate = await _estimate_cycle_usage_costs(
        user_id=user_id,
        period_start=period_start_for_estimate,
        period_end=period_end_for_estimate,
        session=session,
    )
    
    return {
        "polar_sub_id": sub.polar_sub_id,
        "status": sub.status,
        "period_start": period_start,
        "current_period_end": period_end,
        "grace_until": grace_end,
        "plan": user.plan if user else "free",
        "days_remaining": days_remaining,
        "in_grace_period": in_grace_period,
        "billing_model": "postpaid_usage",
        "security_deposit_usd": SECURITY_DEPOSIT_USD,
        "platform_fee_rate": PLATFORM_FEE_RATE,
        "usage_charges_usd": cycle_estimate["usage_charges_usd"],
        "platform_fee_usd": cycle_estimate["platform_fee_usd"],
        "deposit_applied_usd": cycle_estimate["deposit_applied_usd"],
        "estimated_total_due_usd": cycle_estimate["estimated_total_due_usd"],
        "cycle_token_usage": cycle_estimate["cycle_token_usage"],
        "usage_by_model": cycle_estimate["usage_by_model"],
        # Days into the current 30-day prepaid cycle (0-30)
        "days_into_cycle": (
            (datetime.now(tz=timezone.utc) - period_start).days
            if period_start else None
        ),
    }


async def handle_polar_subscription_activated(
    payload: dict[str, Any], session: AsyncSession
) -> None:
    """Process a subscription.activated webhook from Polar."""
    import uuid  # noqa: PLC0415

    sub_data = payload.get("data", {})
    polar_sub_id = sub_data.get("id")
    customer_metadata = sub_data.get("metadata", {})
    user_id = customer_metadata.get("pakalon_user_id")
    if not user_id or not polar_sub_id:
        logger.warning("Missing user_id or polar_sub_id in webhook payload")
        return

    current_period_end_str = sub_data.get("current_period_end")
    current_period_end = (
        datetime.fromisoformat(current_period_end_str)
        if current_period_end_str
        else None
    )

    # Check existing subscription
    result = await session.execute(
        select(Subscription).where(Subscription.polar_sub_id == polar_sub_id)
    )
    existing = result.scalar_one_or_none()

    now = datetime.now(tz=timezone.utc)

    if existing is None:
        new_sub = Subscription(
            id=str(uuid.uuid4()),
            user_id=user_id,
            polar_sub_id=polar_sub_id,
            status="active",
            # Prepaid cycle: period_start is day-0 (the day payment is confirmed)
            period_start=now,
            period_end=current_period_end,
            created_at=now,
        )
        session.add(new_sub)
    else:
        existing.status = "active"
        existing.period_end = current_period_end
        # Preserve existing period_start if already set; otherwise stamp it now
        # (handles edge case where webhook fires before the record has a start date)
        if existing.period_start is None:
            existing.period_start = now

    # Upgrade user plan
    user_result = await session.execute(
        select(User).where(User.id == user_id)
    )
    user = user_result.scalar_one_or_none()
    if user:
        user.plan = "pro"

    await session.flush()


async def handle_polar_subscription_revoked(
    payload: dict[str, Any], session: AsyncSession
) -> None:
    """Process a subscription.revoked webhook — apply grace period."""
    sub_data = payload.get("data", {})
    polar_sub_id = sub_data.get("id")
    if not polar_sub_id:
        return

    result = await session.execute(
        select(Subscription).where(Subscription.polar_sub_id == polar_sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        return

    sub.status = "past_due"
    sub.grace_end = datetime.now(tz=timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)

    # Downgrade user to free after grace period expires (done by nightly job)
    await session.flush()

async def handle_polar_subscription_paused(
    payload: dict[str, Any], session: AsyncSession
) -> None:
    """Process a subscription.paused webhook."""
    sub_data = payload.get("data", {})
    polar_sub_id = sub_data.get("id")
    if not polar_sub_id:
        return

    result = await session.execute(
        select(Subscription).where(Subscription.polar_sub_id == polar_sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        return

    sub.status = "paused"
    await session.flush()

async def handle_polar_subscription_resumed(
    payload: dict[str, Any], session: AsyncSession
) -> None:
    """Process a subscription.resumed webhook."""
    sub_data = payload.get("data", {})
    polar_sub_id = sub_data.get("id")
    if not polar_sub_id:
        return

    result = await session.execute(
        select(Subscription).where(Subscription.polar_sub_id == polar_sub_id)
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        return

    sub.status = "active"
    sub.grace_end = None
    # When a subscription is resumed, stamp a fresh period_start for the new cycle
    if sub.period_start is None:
        sub.period_start = datetime.now(tz=timezone.utc)
    await session.flush()

async def handle_polar_order_refunded_or_disputed(
    payload: dict[str, Any], session: AsyncSession
) -> None:
    """Process an order.refunded or order.disputed webhook."""
    order_data = payload.get("data", {})
    customer_metadata = order_data.get("metadata", {})
    user_id = customer_metadata.get("pakalon_user_id")
    
    if not user_id:
        # Try to find user by email if metadata is missing
        customer_email = order_data.get("customer_email")
        if customer_email:
            user_result = await session.execute(
                select(User).where(User.email == customer_email)
            )
            user = user_result.scalar_one_or_none()
            if user:
                user_id = user.id
                
    if not user_id:
        logger.warning("Could not identify user for refunded/disputed order")
        return

    # Find active subscription for this user and revoke it immediately
    sub_result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status == "active"
        )
    )
    sub = sub_result.scalar_one_or_none()
    if sub:
        sub.status = "canceled"
        sub.grace_end = datetime.now(tz=timezone.utc) # No grace period for refunds/disputes
        
    # Downgrade user immediately
    user_result = await session.execute(
        select(User).where(User.id == user_id)
    )
    user = user_result.scalar_one_or_none()
    if user:
        user.plan = "free"
        
    await session.flush()
