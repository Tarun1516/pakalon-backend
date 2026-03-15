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

from collections import defaultdict
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.message import Message
from app.models.model_usage import ModelUsage
from app.models.session import Session
from app.models.subscription import Subscription
from app.models.user import User
from app.services.heatmap_service import get_contribution_heatmap
from app.services.trial_abuse import remaining_trial_days

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize datetime values to UTC for cross-backend consistency."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    start_date: date | None = Query(default=None, description="Exact inclusive start date override (YYYY-MM-DD)"),
    end_date: date | None = Query(default=None, description="Exact inclusive end date override (YYYY-MM-DD)"),
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
    user_id = current_user.id
    user_email = current_user.email
    user_github_login = current_user.github_login if hasattr(current_user, "github_login") else None
    user_plan = current_user.plan
    user_trial_days_used = current_user.trial_days_used
    user_trial_days_remaining = remaining_trial_days(current_user)

    # ── Derive an accurate account creation timestamp ───────────────────────
    account_created_candidates: list[datetime] = []
    user_created = _ensure_utc(current_user.created_at if hasattr(current_user, "created_at") else None)
    if user_created is not None:
        account_created_candidates.append(user_created)

    first_session_row = await session.execute(
        select(func.min(Session.created_at)).where(Session.user_id == user_id)
    )
    first_session_at = _ensure_utc(first_session_row.scalar_one_or_none())
    if first_session_at is not None:
        account_created_candidates.append(first_session_at)

    first_usage_row = await session.execute(
        select(func.min(ModelUsage.created_at)).where(ModelUsage.user_id == user_id)
    )
    first_usage_at = _ensure_utc(first_usage_row.scalar_one_or_none())
    if first_usage_at is not None:
        account_created_candidates.append(first_usage_at)

    try:
        from app.models.login_event import LoginEvent  # noqa: PLC0415

        first_login_row = await session.execute(
            select(func.min(LoginEvent.created_at)).where(LoginEvent.user_id == user_id)
        )
        first_login_at = _ensure_utc(first_login_row.scalar_one_or_none())
        if first_login_at is not None:
            account_created_candidates.append(first_login_at)
    except Exception as exc:
        logger.warning("Dashboard: could not derive first login timestamp: %s", exc)

    account_created_at = min(account_created_candidates) if account_created_candidates else now

    if start_date or end_date:
        start = start_date or ((now - timedelta(days=days - 1)).date())
        end = end_date or now.date()
        if end < start:
            start, end = end, start
        requested_since = datetime.combine(start, time.min, tzinfo=timezone.utc)
        until = datetime.combine(end, time.max, tzinfo=timezone.utc)
        since = max(requested_since, account_created_at)
        if until < since:
            until = datetime.combine(since.date(), time.max, tzinfo=timezone.utc)
        days = max(1, (until.date() - since.date()).days + 1)
    else:
        requested_since = now - timedelta(days=days - 1)
        since = max(requested_since, account_created_at)
        until = now
        days = max(1, (until.date() - since.date()).days + 1)

    # ── Heatmap ──────────────────────────────────────────────────────────────
    heatmap_data: list[dict] = []
    try:
        heatmap = await get_contribution_heatmap(user_id, session, days=days, end_date=until.date())
        heatmap_data = [
            {"date": day.date.isoformat(), "count": day.count, "level": day.level}
            for day in heatmap.days
        ]
    except Exception as exc:
        logger.warning("Dashboard: heatmap fetch failed: %s", exc)
        await session.rollback()
        # Fall back to raw model_usage counts per day (DB-agnostic, no date_trunc)
        usage_rows = await session.execute(
            select(ModelUsage.created_at)
            .where(
                ModelUsage.user_id == user_id,
                ModelUsage.created_at >= since,
                ModelUsage.created_at <= until,
            )
            .order_by(ModelUsage.created_at.asc())
        )
        daily_counts: dict[str, int] = defaultdict(int)
        for (created_at,) in usage_rows.all():
            created = _ensure_utc(created_at)
            if created is None:
                continue
            day_key = created.date().isoformat()
            daily_counts[day_key] += 1

        for day_key in sorted(daily_counts):
            count = daily_counts[day_key]
            heatmap_data.append({
                "date": day_key,
                "count": count,
                "level": min(4, count // 3),
            })

    # ── Recent sessions ───────────────────────────────────────────────────────
    sessions_rows = await session.execute(
        select(Session)
        .where(
            Session.user_id == user_id,
            Session.created_at >= since,
            Session.created_at <= until,
        )
        .order_by(Session.created_at.desc())
        .limit(50)
    )
    sessions = sessions_rows.scalars().all()
    session_ids = [item.id for item in sessions]
    msg_counts: dict[str, int] = {}
    token_sums: dict[str, int] = {}
    first_prompts: dict[str, str] = {}

    if session_ids:
        msg_result = await session.execute(
            select(Message.session_id, func.count(Message.id))
            .where(Message.session_id.in_(session_ids))
            .group_by(Message.session_id)
        )
        msg_counts = {row[0]: int(row[1] or 0) for row in msg_result.all()}

        usage_result = await session.execute(
            select(ModelUsage.session_id, func.sum(ModelUsage.tokens_used))
            .where(ModelUsage.session_id.in_(session_ids))
            .group_by(ModelUsage.session_id)
        )
        token_sums = {row[0]: int(row[1] or 0) for row in usage_result.all()}

        fallback_prompts: dict[str, str] = {}
        all_messages_result = await session.execute(
            select(Message.session_id, Message.role, Message.content)
            .where(Message.session_id.in_(session_ids))
            .order_by(Message.session_id.asc(), Message.created_at.asc(), Message.id.asc())
        )
        for session_id, role, content in all_messages_result.all():
            text = str(content or "").strip().replace("\n", " ")
            if not text:
                continue
            clipped = text[:200] if len(text) > 200 else text
            if session_id not in fallback_prompts:
                fallback_prompts[session_id] = clipped
            if role == "user" and session_id not in first_prompts:
                first_prompts[session_id] = clipped

        for sid, fallback in fallback_prompts.items():
            first_prompts.setdefault(sid, fallback)

    sessions_list = [
        {
            "id": s.id,
            "title": s.title,
            "prompt_text": first_prompts.get(s.id),
            "model_id": s.model_id,
            "mode": s.mode,
            "project_dir": s.project_dir,
            "lines_added": s.lines_added,
            "lines_deleted": s.lines_deleted,
            "messages_count": msg_counts.get(s.id, 0),
            "tokens_used": token_sums.get(s.id, 0),
            "context_pct_used": float(s.context_pct_used) if s.context_pct_used is not None else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in sessions
    ]

    # ── Per-model usage breakdown ─────────────────────────────────────────────
    model_rows = await session.execute(
        select(
            ModelUsage.model_id,
            func.sum(ModelUsage.tokens_used).label("total_tokens"),
            func.sum(ModelUsage.lines_written).label("total_lines"),
            func.count().label("call_count"),
        )
        .where(
            ModelUsage.user_id == user_id,
            ModelUsage.created_at >= since,
            ModelUsage.created_at <= until,
        )
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
        ).where(
            ModelUsage.user_id == user_id,
            ModelUsage.created_at >= since,
            ModelUsage.created_at <= until,
        )
    )
    totals = totals_row.first()
    total_tokens = int(totals.tokens) if totals else 0
    total_lines = int(totals.lines) if totals else 0

    session_count_row = await session.execute(
        select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
            Session.created_at >= since,
            Session.created_at <= until,
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

    # ── Month-wise token usage for selected window ──────────────────────────
    monthly_token_rows = await session.execute(
        select(ModelUsage.created_at, ModelUsage.tokens_used)
        .where(
            ModelUsage.user_id == user_id,
            ModelUsage.created_at >= since,
            ModelUsage.created_at <= until,
        )
        .order_by(ModelUsage.created_at.asc())
    )
    monthly_totals: dict[str, int] = defaultdict(int)
    for created_at, tokens_used in monthly_token_rows.all():
        created = _ensure_utc(created_at)
        if created is None:
            continue
        month_key = f"{created.year:04d}-{created.month:02d}"
        monthly_totals[month_key] += int(tokens_used or 0)

    monthly_tokens: list[dict[str, Any]] = []
    cursor_year = since.year
    cursor_month = since.month
    final_year = until.year
    final_month = until.month
    while (cursor_year, cursor_month) <= (final_year, final_month):
        month_key = f"{cursor_year:04d}-{cursor_month:02d}"
        monthly_tokens.append({"month": month_key, "tokens": int(monthly_totals.get(month_key, 0))})
        if cursor_month == 12:
            cursor_year += 1
            cursor_month = 1
        else:
            cursor_month += 1

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

    # ── Recent login events ───────────────────────────────────────────────────
    login_events_data: list[dict] = []
    try:
        from app.models.login_event import LoginEvent  # noqa: PLC0415
        login_events_rows = await session.execute(
            select(LoginEvent)
            .where(
                LoginEvent.user_id == user_id,
                LoginEvent.created_at >= account_created_at,
                LoginEvent.created_at <= now,
            )
            .order_by(LoginEvent.created_at.asc())
            .limit(250)
        )
        login_events_data = [
            {
                "id": le.id,
                "login_type": le.login_type,
                "ip_address": str(le.ip_address) if le.ip_address else None,
                "browser": le.browser,
                "os": le.os,
                "device_name": le.device_name,
                "machine_id": le.machine_id,
                "created_at": _ensure_utc(le.created_at).isoformat() if le.created_at else None,
            }
            for le in login_events_rows.scalars()
        ]

        synthetic_machine_id = next((entry.get("machine_id") for entry in login_events_data if entry.get("machine_id")), None)
        account_created_event = {
            "id": f"account-created-{user_id}",
            "login_type": "account_created",
            "ip_address": None,
            "browser": "Supabase / GitHub OAuth",
            "os": login_events_data[0].get("os") if login_events_data else None,
            "device_name": "Account Created",
            "machine_id": synthetic_machine_id,
            "created_at": account_created_at.isoformat(),
        }
        login_events_data.insert(0, account_created_event)
    except Exception as exc:
        logger.warning("Dashboard: login events fetch failed: %s", exc)

    return {
        "user": {
            "id": user_id,
            "email": user_email,
            "github_login": user_github_login,
            "plan": user_plan,
            "trial_days_remaining": user_trial_days_remaining,
            "trial_days_used": user_trial_days_used,
            "created_at": account_created_at.isoformat(),
        },
        "subscription": subscription_data,
        "heatmap": heatmap_data,
        "sessions": sessions_list,
        "model_usage": model_usage,
        "monthly_tokens": monthly_tokens,
        "totals": {
            "tokens": total_tokens,
            "lines": total_lines,
            "sessions": total_sessions,
            "sessions_today": sessions_today,
        },
        "credits": credits_data,
        "login_events": login_events_data,
        "window_days": days,
        "generated_at": now.isoformat(),
    }
