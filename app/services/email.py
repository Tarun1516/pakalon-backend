"""Email service — Resend integration + email queue (T148)."""
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.email_queue import EmailQueue
from app.services.webhook_retry import record_dead_letter

logger = logging.getLogger(__name__)

# Email templates
# For the last 7 days each: enqueue one email per day (day 7 down to day 1)
TRIAL_REMINDER_DAYS = list(range(7, 0, -1))   # [7, 6, 5, 4, 3, 2, 1]
# Outer milestones (14 days) still gets a single heads-up email
TRIAL_EARLY_REMINDER_DAYS = [14]


async def send_email(
    to_email: str,
    subject: str,
    html: str,
    from_email: str | None = None,
) -> bool:
    """Send a transactional email via Resend."""
    settings = get_settings()
    try:
        import resend  # noqa: PLC0415
        resend.api_key = settings.resend_api_key
        resend.Emails.send(
            {
                "from": from_email or settings.email_from,
                "to": [to_email],
                "subject": subject,
                "html": html,
            }
        )
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


def _trial_reminder_html(display_name: str, days_remaining: int, upgrade_url: str) -> str:
    return f"""
<h2>Hi {display_name},</h2>
<p>Your Pakalon free trial has <strong>{days_remaining} day(s) remaining</strong>.</p>
<p>To continue using all features after the trial, upgrade to Pakalon Pro for $22/month.</p>
<p><a href="{upgrade_url}" style="background:#0070f3;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;">
  Upgrade to Pro — $22/mo
</a></p>
<p>Questions? Reply to this email — we're happy to help.</p>
<p>– The Pakalon Team</p>
"""


def _subscription_reminder_html(display_name: str, days_remaining: int, renew_url: str) -> str:
    """T-BACK-09: Email template for pro subscription renewal reminders."""
    return f"""
<h2>Hi {display_name},</h2>
<p>Your Pakalon Pro subscription will renew in <strong>{days_remaining} day(s)</strong>.</p>
<p>Your subscription is set to automatically renew on your billing date.
   If you'd like to manage or cancel your subscription, you can do so from your dashboard.</p>
<p>
  <a href="{renew_url}" style="background:#0070f3;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;">
    Manage Subscription
  </a>
</p>
<p>Thank you for being a Pakalon Pro member!</p>
<p>– The Pakalon Team</p>
"""


async def enqueue_reminder_series(
    user_id: str,
    email: str,
    display_name: str,
    days_remaining: int,
    session: AsyncSession,
) -> None:
    """
    Enqueue trial reminder emails:
    - One "heads-up" email at day 14
    - One individual email for EACH of the last 7 days (day 7 → day 1)

    This means the user gets exactly 1 email per day for the last 7 days
    plus an early reminder at day 14, as per the spec.

    Idempotent: skips if a reminder for that exact day count already exists.
    """
    settings = get_settings()
    upgrade_url = f"{settings.frontend_url}/pricing"

    # Combine early reminder + daily 7-day series
    all_thresholds = TRIAL_EARLY_REMINDER_DAYS + TRIAL_REMINDER_DAYS

    for threshold in all_thresholds:
        if days_remaining == threshold:  # ← only enqueue for TODAY's exact day count
            email_type = f"trial_reminder_{threshold}d"
            # Check if already queued for this exact threshold
            result = await session.execute(
                select(EmailQueue).where(
                    EmailQueue.user_id == user_id,
                    EmailQueue.email_type == email_type,
                    EmailQueue.status.in_(["pending", "sent"]),
                )
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                html = _trial_reminder_html(display_name, days_remaining, upgrade_url)
                queue_item = EmailQueue(
                    id=str(uuid.uuid4()),
                    user_id=user_id,
                    to_email=email,
                    subject=f"⏰ Your Pakalon trial — {days_remaining} day(s) remaining",
                    html=html,
                    email_type=email_type,
                    status="pending",
                    created_at=datetime.now(tz=timezone.utc),
                )
                session.add(queue_item)

    await session.flush()


async def enqueue_subscription_reminder(
    user_id: str,
    email: str,
    display_name: str,
    days_remaining: int,
    session: AsyncSession,
) -> None:
    """
    T-BACK-09: Enqueue a Pro subscription renewal reminder email.

    Only sends when days_remaining == 7. Uses a distinct email_type
    'subscription_reminder_7d' to avoid duplicates across billing cycles.
    """
    if days_remaining > 7:
        return  # only enqueue within 7-day window

    settings = get_settings()
    renew_url = f"{settings.frontend_url}/dashboard/billing"

    # One email per day for each of the last 7 days (day 7 → day 1)
    # email_type includes the day count to produce 7 distinct records
    email_type = f"subscription_reminder_{days_remaining}d"

    # Idempotency: check if already queued for this exact countdown day
    result = await session.execute(
        select(EmailQueue).where(
            EmailQueue.user_id == user_id,
            EmailQueue.email_type == email_type,
            EmailQueue.status.in_(["pending", "sent"]),
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return  # already queued/sent for this day

    html = _subscription_reminder_html(display_name, days_remaining, renew_url)
    queue_item = EmailQueue(
        id=str(uuid.uuid4()),
        user_id=user_id,
        to_email=email,
        subject=f"⏰ Your Pakalon Pro renews in {days_remaining} day(s)",
        html=html,
        email_type=email_type,
        status="pending",
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(queue_item)
    await session.flush()


def _grace_period_warning_html(display_name: str, days_until_grace_end: int, resubscribe_url: str) -> str:
    return f"""
<h2>Hi {display_name},</h2>
<p>Your Pakalon account is currently in its <strong>grace period</strong>, which ends in
<strong>{days_until_grace_end} day(s)</strong>.</p>
<p>After the grace period ends your account will be locked and all stored sessions will
become read-only. Re-subscribe now to keep full access.</p>
<p><a href="{resubscribe_url}" style="background:#e53e3e;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;">
  Re-subscribe — $22/mo
</a></p>
<p>– The Pakalon Team</p>
"""


def _reactivation_html(display_name: str, dashboard_url: str) -> str:
    return f"""
<h2>Welcome back, {display_name}! 🎉</h2>
<p>Your <strong>Pakalon Pro</strong> subscription is now active again.</p>
<p>All your sessions, history, and configurations are exactly as you left them.</p>
<p><a href="{dashboard_url}" style="background:#0070f3;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;">
  Go to Dashboard
</a></p>
<p>– The Pakalon Team</p>
"""


def _account_deleted_html(display_name: str) -> str:
    return f"""
<h2>Your Pakalon account has been deleted</h2>
<p>Hi {display_name},</p>
<p>As requested, your Pakalon account and all associated data have been permanently
deleted. This action cannot be undone.</p>
<p>If this was a mistake or you have questions, please contact
<a href="mailto:support@pakalon.com">support@pakalon.com</a> within 30 days —
after that, recovery is no longer possible.</p>
<p>– The Pakalon Team</p>
"""


async def send_grace_period_warning(
    user_id: str,
    email: str,
    display_name: str,
    days_until_grace_end: int,
    session: AsyncSession,
) -> None:
    """
    Enqueue a grace-period expiry warning email.
    Sent when grace_end - now <= 2 days. Idempotent per day count.
    """
    settings = get_settings()
    resubscribe_url = f"{settings.frontend_url}/pricing"
    email_type = f"grace_warning_{days_until_grace_end}d"

    result = await session.execute(
        select(EmailQueue).where(
            EmailQueue.user_id == user_id,
            EmailQueue.email_type == email_type,
            EmailQueue.status.in_(["pending", "sent"]),
        )
    )
    if result.scalar_one_or_none() is not None:
        return  # already sent for this day

    html = _grace_period_warning_html(display_name, days_until_grace_end, resubscribe_url)
    session.add(
        EmailQueue(
            id=str(uuid.uuid4()),
            user_id=user_id,
            to_email=email,
            subject=f"⚠️ Your Pakalon grace period ends in {days_until_grace_end} day(s)",
            html=html,
            email_type=email_type,
            status="pending",
            created_at=datetime.now(tz=timezone.utc),
        )
    )
    await session.flush()


async def send_reactivation_email(
    to_email: str,
    display_name: str,
) -> bool:
    """
    Send an immediate (non-queued) welcome-back email when a subscription
    reactivates (e.g. Polar subscription.active webhook).
    """
    settings = get_settings()
    dashboard_url = f"{settings.frontend_url}/dashboard"
    html = _reactivation_html(display_name, dashboard_url)
    return await send_email(
        to_email=to_email,
        subject="🎉 Welcome back to Pakalon Pro!",
        html=html,
    )


async def send_account_deleted_email(
    to_email: str,
    display_name: str,
) -> bool:
    """
    Send an immediate (non-queued) confirmation email after account deletion.
    Called from DELETE /users/{id} after PII anonymisation.
    """
    html = _account_deleted_html(display_name)
    return await send_email(
        to_email=to_email,
        subject="Your Pakalon account has been deleted",
        html=html,
    )


async def process_email_queue(session: AsyncSession) -> int:
    """
    Send pending emails from the queue.

    Returns the number of emails sent.
    """
    result = await session.execute(
        select(EmailQueue)
        .where(EmailQueue.status == "pending")
        .order_by(EmailQueue.created_at.asc())
        .limit(50)
    )
    pending = result.scalars().all()
    sent_count = 0

    for item in pending:
        item.status = "sending"
        await session.flush()

        success = await send_email(
            to_email=item.to_email,
            subject=item.subject,
            html=item.html,
        )

        if success:
            item.status = "sent"
            item.sent_at = datetime.now(tz=timezone.utc)
            sent_count += 1
        else:
            item.retry_count = (item.retry_count or 0) + 1
            if item.retry_count >= 3:
                item.status = "failed"
                # Persist a dead-letter record so ops can inspect + replay
                await record_dead_letter(
                    session=session,
                    service="resend",
                    operation="send_email",
                    payload={
                        "to": item.to_email,
                        "subject": item.subject,
                        "email_type": item.email_type,
                        "queue_id": item.id,
                    },
                    error=f"Permanently failed after {item.retry_count} attempts",
                    attempts=item.retry_count,
                )
            else:
                item.status = "pending"  # retry next time

    await session.commit()
    return sent_count
