"""
admin.py — Admin-only router for analytics exports and system management.
Protected by ADMIN_API_KEY header.

Endpoints:
  GET /admin/users                       — list all users (paginated)
  GET /admin/users/export                — CSV export of all users + usage stats
  GET /admin/users/{id}                  — single user detail with subscription + usage
  POST /admin/users/{id}/send-reminder   — manually trigger reminder email
  GET /admin/users/{id}/credits          — view credit ledger for user
  GET /admin/usage-anomalies             — users with >3σ token usage
  GET /admin/sessions/export             — CSV export of all sessions
  GET /admin/telemetry/export            — CSV export of telemetry events
  GET /admin/stats                       — system-wide aggregate statistics
  POST /admin/users/{id}/reset-trial     — admin override: reset trial days
  POST /admin/users/{id}/upgrade         — admin override: set plan
"""
from __future__ import annotations

import csv
import io
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models.model_usage import ModelUsage
from app.models.session import Session
from app.models.subscription import Subscription
from app.models.telemetry_event import TelemetryEvent
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Admin auth guard
# ---------------------------------------------------------------------------

async def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Verify static admin API key."""
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API key not configured",
        )
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin API key",
        )


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/users",
    summary="List all users (paginated)",
    dependencies=[Depends(require_admin)],
)
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200),
    plan: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Paginated list of all users with subscription + usage summary."""
    offset = (page - 1) * page_size
    q = select(User)
    if plan:
        q = q.where(User.plan == plan)
    q = q.order_by(User.created_at.desc()).offset(offset).limit(page_size)

    result = await session.execute(q)
    users = result.scalars().all()

    count_q = select(func.count()).select_from(User)
    if plan:
        count_q = count_q.where(User.plan == plan)
    total = (await session.execute(count_q)).scalar_one()

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "users": [
            {
                "id": u.id,
                "github_login": u.github_login,
                "email": u.email,
                "plan": u.plan,
                "trial_days_used": u.trial_days_used,
                "account_deleted": u.account_deleted,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


@router.get(
    "/users/export",
    summary="Export all users as CSV",
    dependencies=[Depends(require_admin)],
)
async def export_users_csv(session: AsyncSession = Depends(get_session)):
    """
    Download a CSV with all users, their plan, trial days, and aggregate
    token usage. Suitable for spreadsheet analysis.
    """
    # Fetch users
    users_result = await session.execute(
        select(User).order_by(User.created_at.asc())
    )
    users = users_result.scalars().all()

    # Aggregate token usage per user
    usage_q = await session.execute(
        select(
            ModelUsage.user_id,
            func.sum(ModelUsage.input_tokens + ModelUsage.output_tokens).label("total_tokens"),
            func.count().label("request_count"),
        ).group_by(ModelUsage.user_id)
    )
    usage_map: dict[str, dict] = {
        row.user_id: {"total_tokens": row.total_tokens or 0, "request_count": row.request_count}
        for row in usage_q
    }

    # Active subscriptions
    sub_result = await session.execute(
        select(Subscription).where(Subscription.status == "active")
    )
    sub_map: dict[str, Subscription] = {
        s.user_id: s for s in sub_result.scalars().all()
    }

    # Build CSV
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "user_id", "github_login", "email", "display_name", "plan",
        "trial_days_used", "trial_days_remaining", "account_deleted",
        "total_tokens", "request_count", "sub_status", "sub_period_end",
        "created_at",
    ])
    writer.writeheader()

    for u in users:
        usage = usage_map.get(u.id, {"total_tokens": 0, "request_count": 0})
        sub = sub_map.get(u.id)
        trial_remaining = max(0, 30 - u.trial_days_used)

        writer.writerow({
            "user_id": u.id,
            "github_login": u.github_login,
            "email": u.email,
            "display_name": u.display_name or "",
            "plan": u.plan,
            "trial_days_used": u.trial_days_used,
            "trial_days_remaining": trial_remaining,
            "account_deleted": u.account_deleted,
            "total_tokens": usage["total_tokens"],
            "request_count": usage["request_count"],
            "sub_status": sub.status if sub else "",
            "sub_period_end": sub.period_end.isoformat() if sub and sub.period_end else "",
            "created_at": u.created_at.isoformat() if u.created_at else "",
        })

    output.seek(0)
    filename = f"pakalon_users_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Session export
# ---------------------------------------------------------------------------

@router.get(
    "/sessions/export",
    summary="Export all sessions as CSV",
    dependencies=[Depends(require_admin)],
)
async def export_sessions_csv(
    days: int = Query(default=30, description="Days of history to export"),
    session: AsyncSession = Depends(get_session),
):
    """Download a CSV of all sessions within the last N days."""
    from datetime import timedelta
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    result = await session.execute(
        select(Session).where(Session.created_at >= cutoff).order_by(Session.created_at.desc())
    )
    sessions = result.scalars().all()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "session_id", "user_id", "mode", "model_id",
        "message_count", "tokens_used", "created_at", "updated_at",
    ])
    writer.writeheader()

    for s in sessions:
        writer.writerow({
            "session_id": s.id,
            "user_id": s.user_id,
            "mode": getattr(s, "mode", ""),
            "model_id": getattr(s, "model_id", ""),
            "message_count": getattr(s, "message_count", 0),
            "tokens_used": getattr(s, "tokens_used", 0),
            "created_at": s.created_at.isoformat() if s.created_at else "",
            "updated_at": s.updated_at.isoformat() if hasattr(s, "updated_at") and s.updated_at else "",
        })

    output.seek(0)
    filename = f"pakalon_sessions_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Telemetry export
# ---------------------------------------------------------------------------

@router.get(
    "/telemetry/export",
    summary="Export telemetry events as CSV",
    dependencies=[Depends(require_admin)],
)
async def export_telemetry_csv(
    days: int = Query(default=7, description="Days of history to export"),
    event_name: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Download a CSV of telemetry events within the last N days."""
    from datetime import timedelta
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

    q = select(TelemetryEvent).where(TelemetryEvent.created_at >= cutoff)
    if event_name:
        q = q.where(TelemetryEvent.event_name == event_name)
    q = q.order_by(TelemetryEvent.created_at.desc())

    result = await session.execute(q)
    events = result.scalars().all()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "event_id", "user_id", "event_name", "cli_version",
        "os_name", "client_ip", "country", "city", "created_at", "properties",
    ])
    writer.writeheader()

    for ev in events:
        props = ev.properties or {}
        geo = props.get("geo", {})
        writer.writerow({
            "event_id": ev.id,
            "user_id": ev.user_id or "",
            "event_name": ev.event_name,
            "cli_version": ev.cli_version or "",
            "os_name": ev.os_name or "",
            "client_ip": props.get("client_ip", ""),
            "country": geo.get("country_name", ""),
            "city": geo.get("city", ""),
            "created_at": ev.created_at.isoformat() if ev.created_at else "",
            "properties": str({k: v for k, v in props.items() if k not in ("client_ip", "geo")}),
        })

    output.seek(0)
    filename = f"pakalon_telemetry_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Audit log export (T-BE-25)
# ---------------------------------------------------------------------------

@router.get(
    "/audit/export",
    summary="Export audit log rows as CSV",
    dependencies=[Depends(require_admin)],
)
async def export_audit_log_csv(
    from_date: Optional[str] = Query(default=None, alias="from", description="ISO date start filter (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(default=None, alias="to", description="ISO date end filter (YYYY-MM-DD)"),
    user_id: Optional[str] = Query(default=None, description="Filter by user_id"),
    action: Optional[str] = Query(default=None, description="Filter by action prefix"),
    limit: int = Query(default=50000, le=200000, description="Max rows to export"),
    session: AsyncSession = Depends(get_session),
):
    """
    Download a CSV of all audit_log rows filtered by date range, user, or action.

    Query params (all optional):
      - from=YYYY-MM-DD  (inclusive)
      - to=YYYY-MM-DD    (inclusive)
      - user_id          (exact match)
      - action           (prefix match, e.g. subscription.)
      - limit            (default 50 000, max 200 000)
    """
    from app.models.audit_log import AuditLog
    import json

    q = select(AuditLog).order_by(AuditLog.created_at.desc())

    if from_date:
        try:
            from_dt = datetime.fromisoformat(from_date.strip()).replace(tzinfo=timezone.utc)
            q = q.where(AuditLog.created_at >= from_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid from date: {from_date!r}")

    if to_date:
        try:
            to_dt = datetime.fromisoformat(to_date.strip()).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)
            q = q.where(AuditLog.created_at <= to_dt)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid to date: {to_date!r}")

    if user_id:
        q = q.where(AuditLog.user_id == user_id)

    if action:
        q = q.where(AuditLog.action.startswith(action))

    q = q.limit(limit)

    result = await session.execute(q)
    rows = result.scalars().all()

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id", "user_id", "action", "resource_type", "resource_id",
        "ip_address", "extra", "created_at",
    ])
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "id": row.id,
            "user_id": row.user_id or "",
            "action": row.action,
            "resource_type": row.resource_type or "",
            "resource_id": row.resource_id or "",
            "ip_address": row.ip_address or "",
            "extra": json.dumps(row.extra) if row.extra else "",
            "created_at": row.created_at.isoformat() if row.created_at else "",
        })

    output.seek(0)
    date_range = ""
    if from_date or to_date:
        date_range = f"_{from_date or 'begin'}_{to_date or 'end'}"
    filename = f"pakalon_audit{date_range}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------

@router.get(
    "/stats",
    summary="System-wide aggregate statistics",
    dependencies=[Depends(require_admin)],
)
async def get_system_stats(session: AsyncSession = Depends(get_session)):
    """Return high-level aggregate KPIs for the admin dashboard."""
    total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    free_users = (await session.execute(select(func.count()).select_from(User).where(User.plan == "free", User.account_deleted == False))).scalar_one()
    pro_users = (await session.execute(select(func.count()).select_from(User).where(User.plan == "pro"))).scalar_one()
    deleted_users = (await session.execute(select(func.count()).select_from(User).where(User.account_deleted == True))).scalar_one()
    total_sessions = (await session.execute(select(func.count()).select_from(Session))).scalar_one()
    total_tokens = (await session.execute(
        select(func.sum(ModelUsage.input_tokens + ModelUsage.output_tokens))
    )).scalar_one() or 0
    active_subs = (await session.execute(
        select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    )).scalar_one()

    return {
        "users": {
            "total": total_users,
            "free": free_users,
            "pro": pro_users,
            "deleted": deleted_users,
        },
        "subscriptions": {"active": active_subs},
        "sessions": {"total": total_sessions},
        "tokens": {"total": total_tokens},
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Admin overrides
# ---------------------------------------------------------------------------

@router.post(
    "/users/{user_id}/reset-trial",
    summary="Reset a user's trial days used (admin override)",
    dependencies=[Depends(require_admin)],
)
async def reset_trial(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Set trial_days_used back to 0 for a specific user."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.trial_days_used = 0
    await session.commit()
    logger.info("[admin] Trial reset for user %s", user_id)
    return {"ok": True, "user_id": user_id, "trial_days_used": 0}


@router.post(
    "/users/{user_id}/set-plan",
    summary="Set a user's plan (admin override)",
    dependencies=[Depends(require_admin)],
)
async def set_plan(
    user_id: str,
    plan: str = Query(description="Plan: free | pro | enterprise"),
    session: AsyncSession = Depends(get_session),
):
    """Override a user's plan directly."""
    if plan not in ("free", "pro", "enterprise"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan")
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.plan = plan
    await session.commit()
    logger.info("[admin] Plan set to '%s' for user %s", plan, user_id)
    return {"ok": True, "user_id": user_id, "plan": plan}


# ---------------------------------------------------------------------------
# User detail endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/users/{user_id}",
    summary="Get full user detail with subscription and usage",
    dependencies=[Depends(require_admin)],
)
async def get_user_detail(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Return comprehensive user detail: profile, subscription, token usage, and session count."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Subscription
    sub_result = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.created_at.desc()).limit(1)
    )
    sub = sub_result.scalar_one_or_none()

    # Aggregate token usage
    usage_result = await session.execute(
        select(
            func.coalesce(func.sum(ModelUsage.tokens_in), 0),
            func.coalesce(func.sum(ModelUsage.tokens_out), 0),
            func.count(ModelUsage.id),
        ).where(ModelUsage.user_id == user_id)
    )
    tokens_in, tokens_out, usage_count = usage_result.one()

    # Session count
    session_count_result = await session.execute(
        select(func.count(Session.id)).where(Session.user_id == user_id)
    )
    session_count = session_count_result.scalar_one()

    # Credit ledger (imported lazily to avoid circular dep)
    credit_info = None
    try:
        from app.models.credit_ledger import CreditLedger
        credit_result = await session.execute(
            select(CreditLedger).where(CreditLedger.user_id == user_id).order_by(CreditLedger.period_start.desc()).limit(1)
        )
        ledger = credit_result.scalar_one_or_none()
        if ledger:
            credit_info = {
                "credits_total": ledger.credits_total,
                "credits_used": ledger.credits_used,
                "credits_remaining": ledger.credits_remaining,
                "period_start": ledger.period_start.isoformat() if ledger.period_start else None,
                "period_end": ledger.period_end.isoformat() if ledger.period_end else None,
            }
    except Exception:
        pass

    return {
        "id": user.id,
        "github_login": user.github_login,
        "email": user.email,
        "plan": user.plan,
        "trial_days_used": user.trial_days_used,
        "account_deleted": user.account_deleted,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "subscription": {
            "polar_subscription_id": sub.polar_subscription_id if sub else None,
            "status": sub.status if sub else None,
            "plan": sub.plan if sub else None,
            "current_period_end": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
        } if sub else None,
        "usage": {
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
            "total_tokens": int(tokens_in) + int(tokens_out),
            "usage_records": int(usage_count),
        },
        "session_count": int(session_count),
        "credits": credit_info,
    }


# ---------------------------------------------------------------------------
# Send reminder email (manual trigger)
# ---------------------------------------------------------------------------

@router.post(
    "/users/{user_id}/send-reminder",
    summary="Manually trigger a reminder email for a user",
    dependencies=[Depends(require_admin)],
)
async def send_reminder(
    user_id: str,
    reminder_type: str = Query(default="trial_expiring", description="Reminder type: trial_expiring | upgrade_nudge | inactivity"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    session: AsyncSession = Depends(get_session),
):
    """Manually enqueue a reminder email for a specific user."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    valid_types = {"trial_expiring", "upgrade_nudge", "inactivity"}
    if reminder_type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid reminder_type. Must be one of: {', '.join(valid_types)}",
        )

    # Attempt to enqueue via email_queue service
    try:
        from app.jobs.email_queue import enqueue_email
        await enqueue_email(
            session=session,
            user_id=user_id,
            email=user.email or "",
            email_type=reminder_type,
            payload={"user_id": user_id, "plan": getattr(user, "plan", "free"), "manual": True},
        )
        logger.info("[admin] Reminder '%s' manually enqueued for user %s", reminder_type, user_id)
        return {"ok": True, "user_id": user_id, "reminder_type": reminder_type, "queued": True}
    except Exception as exc:
        logger.warning("[admin] Could not enqueue reminder for user %s: %s", user_id, exc)
        return {"ok": False, "user_id": user_id, "reminder_type": reminder_type, "queued": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# User credit ledger view
# ---------------------------------------------------------------------------

@router.get(
    "/users/{user_id}/credits",
    summary="View credit ledger history for a user",
    dependencies=[Depends(require_admin)],
)
async def get_user_credits(
    user_id: str,
    limit: int = Query(default=12, le=24),
    session: AsyncSession = Depends(get_session),
):
    """Return the full credit ledger history for a user (last N billing periods)."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    try:
        from app.services.credits import get_all_ledgers
        ledgers = await get_all_ledgers(user_id, session, limit=limit)
        return {
            "user_id": user_id,
            "email": user.email,
            "plan": getattr(user, "plan", "free"),
            "ledgers": ledgers,
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Usage anomaly detection
# ---------------------------------------------------------------------------

@router.get(
    "/usage-anomalies",
    summary="List users with statistically anomalous token usage (>3σ)",
    dependencies=[Depends(require_admin)],
)
async def get_usage_anomalies(
    sigma_threshold: float = Query(default=3.0, ge=1.0, le=10.0, description="Standard deviation threshold"),
    session: AsyncSession = Depends(get_session),
):
    """
    Identify users whose total token usage exceeds mean + sigma_threshold * stdev.
    Returns flagged users sorted by usage descending.
    """
    # Aggregate total tokens per user
    usage_result = await session.execute(
        select(
            ModelUsage.user_id,
            func.coalesce(func.sum(ModelUsage.tokens_in + ModelUsage.tokens_out), 0).label("total_tokens"),
        ).group_by(ModelUsage.user_id)
    )
    rows = usage_result.all()

    if len(rows) < 3:
        return {"anomalies": [], "message": "Not enough data for statistical analysis (need ≥3 users)"}

    totals = [float(r.total_tokens) for r in rows]
    mean = statistics.mean(totals)
    stdev = statistics.stdev(totals) if len(totals) > 1 else 0.0
    threshold = mean + sigma_threshold * stdev

    # Find anomalous users
    anomalous_user_ids = [
        r.user_id for r in rows if float(r.total_tokens) > threshold
    ]

    if not anomalous_user_ids:
        return {
            "anomalies": [],
            "stats": {"mean": round(mean, 1), "stdev": round(stdev, 1), "threshold": round(threshold, 1)},
        }

    # Fetch user details for flagged users
    users_result = await session.execute(
        select(User).where(User.id.in_(anomalous_user_ids))
    )
    users_map = {u.id: u for u in users_result.scalars().all()}

    usage_map = {r.user_id: float(r.total_tokens) for r in rows}
    anomalies = []
    for uid in anomalous_user_ids:
        u = users_map.get(uid)
        total = usage_map.get(uid, 0.0)
        sigma_distance = (total - mean) / stdev if stdev > 0 else float("inf")
        anomalies.append({
            "user_id": uid,
            "email": u.email if u else None,
            "github_login": u.github_login if u else None,
            "plan": u.plan if u else None,
            "total_tokens": int(total),
            "mean_tokens": round(mean, 1),
            "sigma_distance": round(sigma_distance, 2),
        })

    anomalies.sort(key=lambda x: x["total_tokens"], reverse=True)

    return {
        "anomalies": anomalies,
        "stats": {
            "mean": round(mean, 1),
            "stdev": round(stdev, 1),
            "threshold": round(threshold, 1),
            "sigma_threshold": sigma_threshold,
            "total_users_analyzed": len(rows),
        },
    }


# ---------------------------------------------------------------------------
# T-BE-27: Analytics funnel aggregates (web analytics pipeline)
# ---------------------------------------------------------------------------

@router.get(
    "/analytics/funnels",
    dependencies=[Depends(require_admin)],
    summary="T-BE-27: Conversion funnel aggregates",
    tags=["admin", "analytics"],
)
async def analytics_funnels(
    days: int = Query(default=30, ge=1, le=365, description="Rolling window in days"),
    session: AsyncSession = Depends(get_session),
):
    """
    Conversion funnel: page_view → session_start → upgrade_click → subscription_created.

    Returns per-event counts over the requested rolling window so the team
    can compute conversion rates at each stage.  Also returns top pages by
    view count, top features by usage, and a per-country breakdown.

    Query param:
    - ``days`` (default 30): rolling window in days (max 365)
    """
    from datetime import timedelta  # noqa: PLC0415

    since = datetime.now(tz=timezone.utc) - timedelta(days=days)

    # ── Funnel step counts ──────────────────────────────────────────────────
    funnel_events = [
        "page_view",
        "session_start",
        "feature_usage",
        "cta_click",
        "upgrade_click",
        "subscription.created",
    ]
    funnel_counts: dict[str, int] = {}
    for ev in funnel_events:
        count_row = (
            await session.execute(
                select(func.count(TelemetryEvent.id)).where(
                    TelemetryEvent.event_name == ev,
                    TelemetryEvent.created_at >= since,
                )
            )
        ).scalar_one()
        funnel_counts[ev] = int(count_row or 0)

    # ── Top pages ────────────────────────────────────────────────────────────
    # Properties are JSONB — cast page field for aggregation
    top_pages_rows = (
        await session.execute(
            text(
                """
                SELECT properties->>'page' AS page, COUNT(*) AS cnt
                FROM telemetry_events
                WHERE event_name = 'page_view'
                  AND created_at >= :since
                  AND properties->>'page' != ''
                GROUP BY page
                ORDER BY cnt DESC
                LIMIT 20
                """
            ),
            {"since": since},
        )
    ).all()
    top_pages = [{"page": r[0], "views": int(r[1])} for r in top_pages_rows]

    # ── Top features ────────────────────────────────────────────────────────
    top_features_rows = (
        await session.execute(
            text(
                """
                SELECT properties->>'feature' AS feature, COUNT(*) AS cnt
                FROM telemetry_events
                WHERE event_name = 'feature_usage'
                  AND created_at >= :since
                  AND properties->>'feature' != ''
                GROUP BY feature
                ORDER BY cnt DESC
                LIMIT 20
                """
            ),
            {"since": since},
        )
    ).all()
    top_features = [{"feature": r[0], "count": int(r[1])} for r in top_features_rows]

    # ── Country breakdown ────────────────────────────────────────────────────
    country_rows = (
        await session.execute(
            text(
                """
                SELECT
                    properties->'geo'->>'country_code' AS cc,
                    properties->'geo'->>'country_name' AS name,
                    COUNT(*) AS cnt
                FROM telemetry_events
                WHERE event_name IN ('page_view', 'session_start', 'upgrade_click')
                  AND created_at >= :since
                  AND properties->'geo'->>'country_code' != ''
                GROUP BY cc, name
                ORDER BY cnt DESC
                LIMIT 30
                """
            ),
            {"since": since},
        )
    ).all()
    countries = [{"country_code": r[0], "country_name": r[1], "events": int(r[2])} for r in country_rows]

    # ── Computed conversion rates ────────────────────────────────────────────
    pv = funnel_counts.get("page_view", 0)
    ss = funnel_counts.get("session_start", 0)
    uc = funnel_counts.get("upgrade_click", 0)
    sc = funnel_counts.get("subscription.created", 0)

    conversions = {
        "page_view_to_session_start_pct": round(ss / pv * 100, 2) if pv else 0,
        "session_start_to_upgrade_click_pct": round(uc / ss * 100, 2) if ss else 0,
        "upgrade_click_to_subscription_pct": round(sc / uc * 100, 2) if uc else 0,
        "overall_page_view_to_paid_pct": round(sc / pv * 100, 2) if pv else 0,
    }

    return {
        "window_days": days,
        "since": since.isoformat(),
        "funnel": funnel_counts,
        "conversions": conversions,
        "top_pages": top_pages,
        "top_features": top_features,
        "countries": countries,
    }


@router.get(
    "/analytics/referrers",
    dependencies=[Depends(require_admin)],
    summary="T-BE-27: Top referrer domains for page_view events",
    tags=["admin", "analytics"],
)
async def analytics_referrers(
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    """Top referring domains over the rolling window."""
    from datetime import timedelta  # noqa: PLC0415

    since = datetime.now(tz=timezone.utc) - timedelta(days=days)

    rows = (
        await session.execute(
            text(
                """
                SELECT properties->>'referrer' AS ref, COUNT(*) AS cnt
                FROM telemetry_events
                WHERE event_name = 'page_view'
                  AND created_at >= :since
                  AND properties->>'referrer' != ''
                GROUP BY ref
                ORDER BY cnt DESC
                LIMIT 30
                """
            ),
            {"since": since},
        )
    ).all()

    return {
        "window_days": days,
        "referrers": [{"referrer": r[0], "count": int(r[1])} for r in rows],
    }
