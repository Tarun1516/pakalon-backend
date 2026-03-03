"""
credits.py — Credits Router
────────────────────────────
Public endpoints for the global credits system.

Endpoints:
  GET  /credits/balance         — current credits remaining + plan limits
  GET  /credits/history         — past billing periods (last 12 months)
  POST /admin/credits/{id}/reset — admin: reset credits for a user (admin key required)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.user import User
from app.routers.admin import require_admin
from app.services import credits as credit_svc

logger = logging.getLogger(__name__)
router = APIRouter(tags=["credits"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class CreditBalanceResponse(BaseModel):
    user_id: str
    plan: str
    credits_total: int
    credits_used: int
    credits_remaining: int
    period_start: str
    period_end: str


class CreditHistoryEntry(BaseModel):
    period_start: str
    period_end: str
    plan: str
    credits_total: int
    credits_used: int
    credits_remaining: int


class AdminResetRequest(BaseModel):
    plan: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get(
    "/credits/balance",
    response_model=CreditBalanceResponse,
    summary="Get current credit balance",
)
async def get_credit_balance(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CreditBalanceResponse:
    """Return the credit balance for the current billing period."""
    ledger = await credit_svc.get_or_create_ledger(
        user_id=current_user.id,
        plan=current_user.plan,
        session=session,
    )
    await session.commit()
    return CreditBalanceResponse(
        user_id=current_user.id,
        plan=ledger.plan,
        credits_total=ledger.credits_total,
        credits_used=ledger.credits_used,
        credits_remaining=ledger.credits_remaining,
        period_start=ledger.period_start.isoformat(),
        period_end=ledger.period_end.isoformat(),
    )


@router.get(
    "/credits/history",
    response_model=list[CreditHistoryEntry],
    summary="Get credit usage history (last 12 months)",
)
async def get_credit_history(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[CreditHistoryEntry]:
    """Return up to 12 months of credit ledger history for the authenticated user."""
    ledgers = await credit_svc.get_all_ledgers(
        user_id=current_user.id,
        session=session,
        limit=12,
    )
    return [
        CreditHistoryEntry(
            period_start=l.period_start.isoformat(),
            period_end=l.period_end.isoformat(),
            plan=l.plan,
            credits_total=l.credits_total,
            credits_used=l.credits_used,
            credits_remaining=l.credits_remaining,
        )
        for l in ledgers
    ]


@router.post(
    "/admin/credits/{user_id}/reset",
    response_model=CreditBalanceResponse,
    summary="Admin: reset a user's credits for the current period",
    dependencies=[Depends(require_admin)],
)
async def admin_reset_credits(
    user_id: str,
    body: AdminResetRequest,
    session: AsyncSession = Depends(get_session),
) -> CreditBalanceResponse:
    """
    Force-reset a user's credit ledger for the current billing period.
    Useful after plan upgrades or manual credit grants.
    """
    ledger = await credit_svc.reset_period(
        user_id=user_id,
        plan=body.plan,
        session=session,
    )
    await session.commit()
    logger.info("Admin reset credits for user=%s plan=%s", user_id, body.plan)
    return CreditBalanceResponse(
        user_id=user_id,
        plan=ledger.plan,
        credits_total=ledger.credits_total,
        credits_used=ledger.credits_used,
        credits_remaining=ledger.credits_remaining,
        period_start=ledger.period_start.isoformat(),
        period_end=ledger.period_end.isoformat(),
    )


# ── Startup check ─────────────────────────────────────────────────────────────

class StartupCheckResponse(BaseModel):
    can_interact: bool
    credits_remaining: int
    plan: str
    reason: str | None = None


@router.get(
    "/credits/startup-check",
    response_model=StartupCheckResponse,
    summary="Check whether the authenticated user can interact with the app",
)
async def startup_credit_check(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StartupCheckResponse:
    """
    Called by the CLI on startup to determine if the user has credits left.

    - If the user is on a paid plan and has credits_remaining > 0: can_interact=True.
    - If credits_remaining == 0: can_interact=False with a blocking reason message.
    - Free-plan users are checked against trial limits.
    """
    ledger = await credit_svc.get_or_create_ledger(
        user_id=current_user.id,
        plan=current_user.plan,
        session=session,
    )
    await session.commit()

    if ledger.credits_remaining <= 0:
        return StartupCheckResponse(
            can_interact=False,
            credits_remaining=0,
            plan=ledger.plan,
            reason=(
                "You have no credits remaining for this billing period. "
                "Upgrade your plan or wait for the next billing cycle to continue."
            ),
        )

    return StartupCheckResponse(
        can_interact=True,
        credits_remaining=ledger.credits_remaining,
        plan=ledger.plan,
    )
