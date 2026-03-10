"""Usage router — trial + subscription status + analytics (T045, T-BACK-02)."""
import logging
import asyncio
import json
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.contribution_heatmap import ContributionHeatmap
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.usage import ContributionDay, DailyLines, DailyTokens, HeatmapResponse, UsageResponse
from app.services.heatmap_service import get_yearly_contribution_heatmap
from app.services.trial_abuse import remaining_trial_days
from app.services.usage_analytics import get_usage_analytics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/usage", tags=["usage"])


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize DB datetimes so SQLite tests and Postgres behave consistently."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@router.get(
    "",
    response_model=UsageResponse,
    summary="Get current usage and subscription status",
)
async def get_usage(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Returns trial / subscription status + full analytics for the authenticated user.

    CLI uses this to show the status bar and gate pro features.
    """
    sub_result = await session.execute(
        select(Subscription).where(
            Subscription.user_id == current_user.id,
            Subscription.status == "active",
        )
    )
    active_sub = sub_result.scalar_one_or_none()
    now = datetime.now(tz=timezone.utc)
    current_period_start = _ensure_utc(active_sub.period_start) if active_sub else None
    current_period_end = _ensure_utc(active_sub.period_end) if active_sub else None
    grace_end = _ensure_utc(active_sub.grace_end) if active_sub else None
    is_grace_eligible_status = (
        active_sub is not None
        and active_sub.status in {"canceled", "revoked", "past_due", "paused", "unpaid"}
    )
    is_in_grace_period = bool(
        is_grace_eligible_status
        and grace_end is not None
        and grace_end > now
    )

    analytics = await get_usage_analytics(current_user.id, session)

    return UsageResponse(
        user_id=current_user.id,
        plan=current_user.plan,
        trial_days_used=current_user.trial_days_used,
        trial_days_remaining=remaining_trial_days(current_user),
        subscription_id=active_sub.polar_sub_id if active_sub else None,
        subscription_status=active_sub.status if active_sub else None,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
        days_into_cycle=(
            (now - current_period_start).days
            if current_period_start else None
        ),
        is_in_grace_period=is_in_grace_period,
        grace_period_warning=is_in_grace_period,
        grace_days_remaining=(
            max(0, (grace_end - now).days)
            if is_in_grace_period and grace_end
            else 0
        ),
        total_tokens=analytics["total_tokens"],
        tokens_by_model=analytics["tokens_by_model"],
        daily_tokens=[
            DailyTokens(date=d["date"], tokens=d["tokens"])
            for d in analytics["daily_tokens"]
        ],
        daily_lines_written=[
            DailyLines(date=d["date"], lines=d["lines"])
            for d in analytics["daily_lines_written"]
        ],
        lines_written=analytics["lines_written"],
        sessions_count=analytics["sessions_count"],
    )


@router.get(
    "/heatmap",
    response_model=HeatmapResponse,
    summary="Get contribution heatmap data for a year",
)
async def get_heatmap(
    year: int = Query(default=None, description="Year to get contributions for (default: current year)"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Returns contribution heatmap data for GitHub-style visualization.
    Includes lines added/deleted, commits, tokens used, and sessions per day.
    """
    if year is None:
        year = datetime.now(tz=timezone.utc).year

    data = await get_yearly_contribution_heatmap(current_user.id, year, session)

    return HeatmapResponse(
        year=data["year"],
        contributions=[
            ContributionDay(
                date=c["date"],
                lines_added=c["lines_added"],
                lines_deleted=c["lines_deleted"],
                commits=c["commits"],
                tokens_used=c["tokens_used"],
                sessions_count=c["sessions_count"],
                level=c["level"],
            )
            for c in data["contributions"]
        ],
        total_lines_added=data["total_lines_added"],
        total_lines_deleted=data["total_lines_deleted"],
        total_commits=data.get("total_commits", 0),
        total_tokens=data.get("total_tokens", 0),
    )


@router.websocket("/stream")
async def usage_stream(
    websocket: WebSocket,
    token: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    """
    WebSocket endpoint for real-time context usage streaming.
    """
    from app.middleware.auth import verify_pakalon_jwt, get_user_from_token
    from app.main import redis_client
    
    try:
        payload = verify_pakalon_jwt(token)
        user = await get_user_from_token(payload, session)
    except Exception as e:
        logger.warning(f"WebSocket auth failed: {e}")
        await websocket.close(code=1008, reason="Invalid token")
        return

    await websocket.accept()

    if not redis_client:
        logger.error("Redis client not available for WebSocket")
        await websocket.close(code=1011, reason="Redis not available")
        return

    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"user:{user.id}:usage")

    # T-BE-17: use a short poll tick (0.2 s) so we never block for 5 s
    # while a message is sitting in Redis. A separate ping counter tracks
    # the 5-second keepalive independent of the message receive loop.
    PING_INTERVAL = 5.0   # seconds between keepalive pings
    POLL_TICK = 0.2       # seconds between Redis poll attempts
    last_ping = asyncio.get_event_loop().time()

    try:
        while True:
            # Wait for the next tick (short) or disconnect
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=POLL_TICK)
                # Client sent something — ignore (one-directional stream)
            except asyncio.TimeoutError:
                pass  # normal — just a tick
            except WebSocketDisconnect:
                break

            # Drain all queued Redis messages without sleeping
            drained = 0
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0)
                if not message:
                    break
                await websocket.send_text(message["data"])
                drained += 1
                if drained > 50:  # safety valve
                    break

            # Keepalive ping every PING_INTERVAL seconds
            now = asyncio.get_event_loop().time()
            if now - last_ping >= PING_INTERVAL:
                await websocket.send_json({"type": "ping"})
                last_ping = now

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for user {user.id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        await pubsub.unsubscribe(f"user:{user.id}:usage")

