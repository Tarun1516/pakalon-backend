"""Trial expiry checker + grace period enforcer + sub expiry notifier (T149, T-BACK-03, T-BACK-09)."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.models.subscription import Subscription
from app.models.user import User
from app.services.trial_abuse import TRIAL_DAYS, increment_trial_days, is_trial_expiring_soon
from app.services.email import enqueue_reminder_series, enqueue_subscription_reminder, send_email, send_grace_period_warning

logger = logging.getLogger(__name__)


async def run_expiry_checker(session: AsyncSession | None = None) -> None:
    """
    Nightly scheduled job (cron 0:30 AM).

    Tasks:
    1. Increment trial_days_used for all active free users
    2. Enqueue reminder emails for users nearing trial expiry
    3. Downgrade pro users whose grace period has elapsed
    4. Send 7-day warning emails for expiring subscriptions (T-BACK-03)
    """
    from app.config import get_settings  # noqa: PLC0415
    from app.database import create_async_engine  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    own_session = session is None
    if own_session:
        settings = get_settings()
        engine = create_async_engine(settings.database_url, echo=False)
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
        session = _session_factory()

    try:
        await _increment_trial_days(session)
        await _enforce_grace_period_expiry(session)
        await _notify_expiring_subscriptions(session)
        await session.commit()
    except Exception as exc:
        logger.exception("Expiry checker job failed: %s", exc)
        if not own_session:
            raise
    finally:
        if own_session:
            await session.close()  # type: ignore[union-attr]


async def _increment_trial_days(session: AsyncSession) -> None:
    """Add 1 day to trial_days_used for all non-deleted free users still in trial."""
    result = await session.execute(
        select(User).where(
            User.plan == "free",
            User.account_deleted == False,  # noqa: E712  # fixed: was is_deleted
            User.trial_days_used < TRIAL_DAYS,
        )
    )
    users = result.scalars().all()
    now = datetime.now(tz=timezone.utc)

    for user in users:
        increment_trial_days(user, days=1)

        # Enqueue reminder emails and create in-app notification for trial expiry
        if user.email and is_trial_expiring_soon(user, threshold_days=14):
            days_remaining = TRIAL_DAYS - user.trial_days_used
            await enqueue_reminder_series(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name or user.github_login or "there",
                days_remaining=days_remaining,
                session=session,
            )
            session.add(Notification(
                id=str(uuid.uuid4()),
                user_id=user.id,
                notification_type="billing_reminder",
                title=f"Your free trial ends in {days_remaining} day{'s' if days_remaining != 1 else ''}",
                body=(
                    f"You have {days_remaining} day{'s' if days_remaining != 1 else ''} left on your "
                    "Pakalon free trial. Upgrade to Pro to keep full access."
                ),
                action_url="/upgrade",
                action_label="Upgrade to Pro",
                expires_at=now + timedelta(days=7),
            ))

    await session.flush()
    logger.info("Incremented trial days for %d free users", len(users))


async def _enforce_grace_period_expiry(session: AsyncSession) -> None:
    """Downgrade users whose Pro grace period has elapsed."""
    now = datetime.now(tz=timezone.utc)

    result = await session.execute(
        select(Subscription).where(
            Subscription.status == "past_due",
            Subscription.grace_end <= now,
        )
    )
    expired_subs = result.scalars().all()

    for sub in expired_subs:
        sub.status = "expired"
        # Downgrade user to free plan
        user_result = await session.execute(
            select(User).where(User.id == sub.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user:
            user.plan = "free"
            logger.info("Downgraded user %s to free plan (grace period expired)", user.id)
            # In-app notification for grace period expiry / plan downgrade
            session.add(Notification(
                id=str(uuid.uuid4()),
                user_id=user.id,
                notification_type="grace_period",
                title="Your Pro plan has been downgraded",
                body=(
                    "Your grace period has ended and your account has been moved to the free plan. "
                    "Upgrade to Pro to restore full access."
                ),
                action_url="/upgrade",
                action_label="Upgrade to Pro",
            ))

    await session.flush()
    logger.info("Processed %d expired grace periods", len(expired_subs))

    # Send grace-period warnings to accounts expiring within 2 days
    now_w = datetime.now(tz=timezone.utc)
    warning_window = now_w + timedelta(days=2)
    warn_result = await session.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.status == "past_due",
            Subscription.grace_end > now_w,
            Subscription.grace_end <= warning_window,
        )
    )
    warn_rows = warn_result.all()
    for sub, user in warn_rows:
        if not user.email:
            continue
        days_left = max(1, (sub.grace_end - now_w).days)
        display = user.display_name or user.github_login or "there"
        await send_grace_period_warning(
            user_id=user.id,
            email=user.email,
            display_name=display,
            days_until_grace_end=days_left,
            session=session,
        )
        # In-app notification for grace period warning
        session.add(Notification(
            id=str(uuid.uuid4()),
            user_id=user.id,
            notification_type="grace_period",
            title=f"Pro access ending in {days_left} day{'s' if days_left != 1 else ''}",
            body=(
                f"Your Pro subscription grace period ends in {days_left} day{'s' if days_left != 1 else ''}. "
                "Update your payment method to stay on Pro."
            ),
            action_url="/billing",
            action_label="Update Payment",
            expires_at=now_w + timedelta(days=days_left + 1),
        ))
    logger.info("Queued %d grace-period warning emails", len(warn_rows))


async def _notify_expiring_subscriptions(session: AsyncSession) -> None:
    """
    T-BACK-03 / T-BACK-09: Queue 7-day subscription renewal reminder emails
    for Pro users whose subscription period ends within 7 days.
    Uses enqueue_subscription_reminder() to deduplicate via EmailQueue.
    """
    now = datetime.now(tz=timezone.utc)
    window_end = now + timedelta(days=7)

    result = await session.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(
            Subscription.status.in_(["active", "past_due"]),
            Subscription.period_end >= now,
            Subscription.period_end <= window_end,
        )
    )
    rows = result.all()

    queued_count = 0
    for sub, user in rows:
        if not user.email:
            continue
        days_left = max(1, (sub.period_end - now).days)
        display = user.display_name or user.github_login or "there"
        await enqueue_subscription_reminder(
            user_id=user.id,
            email=user.email,
            display_name=display,
            days_remaining=days_left,
            session=session,
        )
        # In-app notification alongside the email reminder
        session.add(Notification(
            id=str(uuid.uuid4()),
            user_id=user.id,
            notification_type="billing_reminder",
            title=f"Your Pro subscription renews in {days_left} day{'s' if days_left != 1 else ''}",
            body=(
                f"Your Pakalon Pro subscription will renew in {days_left} day{'s' if days_left != 1 else ''}. "
                "Ensure your payment method is up to date."
            ),
            action_url="/billing",
            action_label="Manage Billing",
            expires_at=now + timedelta(days=days_left + 1),
        ))
        queued_count += 1
        logger.info(
            "Queued subscription renewal reminder for user %s (sub %s, expires %s, days_left=%d)",
            user.id, sub.id, sub.period_end, days_left,
        )

    logger.info("Queued %d subscription renewal reminders", queued_count)

