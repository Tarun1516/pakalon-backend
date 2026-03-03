"""
credits.py — Global Credits Service
────────────────────────────────────
Manages per-user credit allocations across billing periods.

Plan credit limits (per period):
  free        →   0  (no credits; token-based free tier only)
  trial       →  50
  pro         → 500
  enterprise  → 5000

Public API:
  get_or_create_ledger(user_id, plan, session) → CreditLedger
  get_remaining(user_id, session)              → int
  debit_credits(user_id, amount, session)      → CreditLedger (raises InsufficientCreditsError)
  reset_period(user_id, plan, session)         → CreditLedger
  get_all_ledgers(user_id, session)            → list[CreditLedger]
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credit_ledger import CreditLedger

# ── Plan limits ───────────────────────────────────────────────────────────────

PLAN_CREDITS: dict[str, int] = {
    "free": 0,
    "trial": 50,
    "pro": 500,
    "enterprise": 5000,
}


class InsufficientCreditsError(Exception):
    """Raised when a debit would exceed the credit balance."""

    def __init__(self, remaining: int, requested: int) -> None:
        self.remaining = remaining
        self.requested = requested
        super().__init__(
            f"Not enough credits: {remaining} remaining, {requested} requested"
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _period_bounds() -> tuple[datetime, datetime]:
    """Return the start/end of the current calendar month (UTC)."""
    now = datetime.now(tz=timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # Advance to next month
    if now.month == 12:
        period_end = period_start.replace(year=now.year + 1, month=1)
    else:
        period_end = period_start.replace(month=now.month + 1)
    return period_start, period_end


# ── Core service functions ────────────────────────────────────────────────────

async def get_or_create_ledger(
    user_id: str,
    plan: str,
    session: AsyncSession,
) -> CreditLedger:
    """
    Return the active credit ledger for the current billing period.
    Creates a new row if none exists for this (user_id, period_start).
    """
    period_start, period_end = _period_bounds()

    result = await session.execute(
        select(CreditLedger)
        .where(CreditLedger.user_id == user_id)
        .where(CreditLedger.period_start == period_start)
    )
    ledger: Optional[CreditLedger] = result.scalar_one_or_none()

    if ledger is None:
        credits_total = PLAN_CREDITS.get(plan, 0)
        ledger = CreditLedger(
            id=str(uuid.uuid4()),
            user_id=user_id,
            plan=plan,
            credits_total=credits_total,
            credits_used=0,
            period_start=period_start,
            period_end=period_end,
        )
        session.add(ledger)
        await session.flush()

    return ledger


async def get_remaining(user_id: str, session: AsyncSession) -> int:
    """
    Return credits remaining for the current period.
    Returns 0 if no ledger exists (free tier with no credits).
    """
    period_start, _ = _period_bounds()
    result = await session.execute(
        select(CreditLedger)
        .where(CreditLedger.user_id == user_id)
        .where(CreditLedger.period_start == period_start)
    )
    ledger = result.scalar_one_or_none()
    return ledger.credits_remaining if ledger else 0


async def debit_credits(
    user_id: str,
    amount: int,
    session: AsyncSession,
    plan: str = "free",
) -> CreditLedger:
    """
    Debit `amount` credits from the user's ledger.

    - Creates a ledger row if one does not exist.
    - Raises `InsufficientCreditsError` if balance would go negative.
    - Plans with `credits_total == 0` are exempt (unlimited / no credit system).
    """
    ledger = await get_or_create_ledger(user_id, plan, session)

    # Plans with 0 total credits bypass the credit system
    if ledger.credits_total == 0:
        return ledger

    remaining = ledger.credits_remaining
    if remaining < amount:
        raise InsufficientCreditsError(remaining=remaining, requested=amount)

    ledger.credits_used += amount
    ledger.updated_at = datetime.now(tz=timezone.utc)
    await session.flush()
    return ledger


async def reset_period(
    user_id: str,
    plan: str,
    session: AsyncSession,
) -> CreditLedger:
    """
    Force-create a new ledger row for the current period with full credits.
    Used by admin overrides or plan upgrades mid-period.
    """
    period_start, period_end = _period_bounds()

    # Expire existing row for this period if it exists
    result = await session.execute(
        select(CreditLedger)
        .where(CreditLedger.user_id == user_id)
        .where(CreditLedger.period_start == period_start)
    )
    existing = result.scalar_one_or_none()
    if existing:
        await session.delete(existing)
        await session.flush()

    credits_total = PLAN_CREDITS.get(plan, 0)
    ledger = CreditLedger(
        id=str(uuid.uuid4()),
        user_id=user_id,
        plan=plan,
        credits_total=credits_total,
        credits_used=0,
        period_start=period_start,
        period_end=period_end,
    )
    session.add(ledger)
    await session.flush()
    return ledger


async def get_all_ledgers(
    user_id: str,
    session: AsyncSession,
    limit: int = 12,
) -> list[CreditLedger]:
    """Return the last `limit` credit ledger entries for the user (most recent first)."""
    result = await session.execute(
        select(CreditLedger)
        .where(CreditLedger.user_id == user_id)
        .order_by(CreditLedger.period_start.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
