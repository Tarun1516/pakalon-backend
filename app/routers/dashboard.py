"""
dashboard.py — Unified dashboard endpoint for the Pakalon web UI.

GET /dashboard/stats
    Returns a single JSON response with all data the web dashboard needs:
      - contribution heatmap (last 365 days)
      - recent sessions list
      - per-model token usage breakdown
      - aggregate totals (tokens, lines, sessions, spend estimate)
      - subscription status
      - credit balance

This avoids waterfall requests from the web UI and reduces DX friction.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.contribution_heatmap import ContributionHeatmap
from app.models.model_usage import ModelUsage
from app.models.session import Session
from app.models.subscription import Subscription
from app.models.user import User
from app.services.heatmap_service import get_contribution_heatmap
from app.services.trial_abuse import remaining_trial_days

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Main unified stats endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/stats",
    summary="Unified dashboard stats (heatmap + sessions + models + totals)",
    response_model=dict,
)
async def get_dashboard_stats(
    days: int = Query(default=365, ge=7, le=730, description="Heatmap / history window in days"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """
    Returns all data the web dashboard needs in a single request:

    ```json
    {
      "user": { "id", "email", "plan", "trial_days_remaining", ... },
      "subscription": { "status", "period_end", ... } | null,
      "heatmap": [ { "date": "YYYY-MM-DD", "count": N, "level": 0-4 }, ... ],
      "sessions": [ { "id", "title", "model_id", "created_at", "lines_added", ... }, ... ],
      "model_usage": [ { "model_id", "total_tokens", "total_lines", "call_count" }, ... ],
      "totals": { "tokens": N, "lines": N, "sessions": N, "sessions_today": N },
      "credits": { "balance": N } | null
    }
    ```
    """
    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(days=days)
    user_id = current_user.id

    # ── Heatmap ──────────────────────────────────────────────────────────────
    heatmap_data: list[dict] = []
    try:
        heatmap = await get_contribution_heatmap(user_id, session, days=days)
        heatmap_data = [
            {"date": day.date.isoformat(), "count": day.count, "level": day.level}
            for day in heatmap.days
        ]
    except Exception as exc:
        logger.warning("Dashboard: heatmap fetch failed: %s", exc)
        # Fall back to raw model_usage counts per day
        rows = await session.execute(
            select(
                func.date_trunc("day", ModelUsage.created_at).label("day"),
                func.count().label("count"),
            )
            .where(ModelUsage.user_id == user_id, ModelUsage.created_at >= since)
            .group_by("day")
            .order_by("day")
        )
        for row in rows:
            heatmap_data.append({
                "date": row.day.date().isoformat(),
                "count": row.count,
                "level": min(4, row.count // 3),
            })

    # ── Recent sessions ───────────────────────────────────────────────────────
    sessions_rows = await session.execute(
        select(Session)
        .where(Session.user_id == user_id, Session.created_at >= since)
        .order_by(Session.created_at.desc())
        .limit(50)
    )
    sessions_list = [
        {
            "id": s.id,
            "title": s.title,
            "model_id": s.model_id,
            "mode": s.mode,
            "project_dir": s.project_dir,
            "lines_added": s.lines_added,
            "lines_deleted": s.lines_deleted,
            "context_pct_used": float(s.context_pct_used) if s.context_pct_used is not None else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in sessions_rows.scalars()
    ]

    # ── Per-model usage breakdown ─────────────────────────────────────────────
    model_rows = await session.execute(
        select(
            ModelUsage.model_id,
            func.sum(ModelUsage.tokens_used).label("total_tokens"),
            func.sum(ModelUsage.lines_written).label("total_lines"),
            func.count().label("call_count"),
        )
        .where(ModelUsage.user_id == user_id, ModelUsage.created_at >= since)
        .group_by(ModelUsage.model_id)
        .order_by(func.sum(ModelUsage.tokens_used).desc())
    )
    model_usage = [
        {
            "model_id": row.model_id,
            "total_tokens": int(row.total_tokens or 0),
            "total_lines": int(row.total_lines or 0),
            "call_count": int(row.call_count or 0),
        }
        for row in model_rows
    ]

    # ── Aggregate totals ──────────────────────────────────────────────────────
    totals_row = await session.execute(
        select(
            func.coalesce(func.sum(ModelUsage.tokens_used), 0).label("tokens"),
            func.coalesce(func.sum(ModelUsage.lines_written), 0).label("lines"),
        ).where(ModelUsage.user_id == user_id, ModelUsage.created_at >= since)
    )
    totals = totals_row.first()
    total_tokens = int(totals.tokens) if totals else 0
    total_lines = int(totals.lines) if totals else 0

    session_count_row = await session.execute(
        select(func.count()).select_from(Session).where(
            Session.user_id == user_id, Session.created_at >= since
        )
    )
    total_sessions = session_count_row.scalar() or 0

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sessions_today_row = await session.execute(
        select(func.count()).select_from(Session).where(
            Session.user_id == user_id, Session.created_at >= today_start
        )
    )
    sessions_today = sessions_today_row.scalar() or 0

    # ── Subscription ─────────────────────────────────────────────────────────
    sub_row = await session.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status.in_(["active", "past_due"]),
        ).order_by(Subscription.created_at.desc()).limit(1)
    )
    sub = sub_row.scalar_one_or_none()
    subscription_data = None
    if sub:
        subscription_data = {
            "id": sub.id,
            "polar_sub_id": sub.polar_sub_id,
            "status": sub.status,
            "plan": sub.plan,
            "period_start": sub.period_start.isoformat() if sub.period_start else None,
            "period_end": sub.period_end.isoformat() if sub.period_end else None,
        }

    # ── Credits ───────────────────────────────────────────────────────────────
    credits_data: dict | None = None
    try:
        from app.models.credit_ledger import CreditLedger  # noqa: PLC0415
        from sqlalchemy import case  # noqa: PLC0415
        credit_row = await session.execute(
            select(
                func.coalesce(
                    func.sum(case((CreditLedger.amount > 0, CreditLedger.amount), else_=0)),
                    0,
                ).label("purchased"),
                func.coalesce(
                    func.sum(case((CreditLedger.amount < 0, CreditLedger.amount), else_=0)),
                    0,
                ).label("used"),
            ).where(CreditLedger.user_id == user_id)
        )
        cred = credit_row.first()
        if cred:
            balance = int(cred.purchased) + int(cred.used)
            credits_data = {"balance": max(0, balance)}
    except Exception:
        pass

    return {
        "user": {
            "id": current_user.id,
            "email": current_user.email,
            "github_login": current_user.github_login if hasattr(current_user, "github_login") else None,
            "plan": current_user.plan,
            "trial_days_remaining": remaining_trial_days(current_user),
            "trial_days_used": current_user.trial_days_used,
            "created_at": current_user.created_at.isoformat() if hasattr(current_user, "created_at") and current_user.created_at else None,
        },
        "subscription": subscription_data,
        "heatmap": heatmap_data,
        "sessions": sessions_list,
        "model_usage": model_usage,
        "totals": {
            "tokens": total_tokens,
            "lines": total_lines,
            "sessions": total_sessions,
            "sessions_today": sessions_today,
        },
        "credits": credits_data,
        "window_days": days,
        "generated_at": now.isoformat(),
    }
