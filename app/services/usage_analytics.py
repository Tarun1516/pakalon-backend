"""Model usage tracking service (T-BACK-01)."""
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.model_usage import ModelUsage

logger = logging.getLogger(__name__)


async def record_model_usage(
    *,
    user_id: str,
    model_id: str,
    tokens_used: int,
    context_window_size: int,
    context_window_used: int,
    lines_written: int = 0,
    session_id: str | None = None,
    db: AsyncSession,
) -> ModelUsage:
    """Insert a new model usage record and return it."""
    record = ModelUsage(
        user_id=user_id,
        session_id=session_id,
        model_id=model_id,
        tokens_used=tokens_used,
        context_window_size=context_window_size,
        context_window_used=context_window_used,
        lines_written=lines_written,
    )
    db.add(record)
    await db.flush()

    # Publish to Redis for real-time context updates
    try:
        from app.main import redis_client
        import json
        if redis_client:
            pct = max(0, 100 - round(context_window_used / context_window_size * 100)) if context_window_size > 0 else None
            payload = {
                "type": "context_update",
                "model_id": model_id,
                "session_id": session_id,
                "tokens_used": tokens_used,
                "context_window_used": context_window_used,
                "context_window_size": context_window_size,
                "remaining_pct": pct
            }
            await redis_client.publish(f"user:{user_id}:usage", json.dumps(payload))
    except Exception as e:
        logger.warning(f"Failed to publish usage to Redis: {e}")

    return record


async def get_remaining_pct(
    user_id: str,
    model_id: str,
    db: AsyncSession,
) -> int | None:
    """
    Return the percentage of context window remaining for the given model,
    based on the most recent usage record (0–100).
    Returns None if no usage exists yet.
    """
    result = await db.execute(
        select(ModelUsage)
        .where(
            ModelUsage.user_id == user_id,
            ModelUsage.model_id == model_id,
            ModelUsage.context_window_size > 0,
        )
        .order_by(ModelUsage.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    used = row.context_window_used
    total = row.context_window_size
    if total == 0:
        return None
    return max(0, 100 - round(used / total * 100))


async def is_context_exhausted(
    user_id: str,
    model_id: str,
    db: AsyncSession,
) -> bool:
    """
    Return True if the user's context window for model_id is exhausted (0%).

    T-BACK-06 / T-BACK-09: Used by the backend to gate new AI calls.
    Returns False if no usage recorded yet (context not exhausted by default).
    """
    pct = await get_remaining_pct(user_id, model_id, db)
    if pct is None:
        return False  # No usage yet — not exhausted
    return pct == 0


async def get_context_status(
    user_id: str,
    model_id: str,
    db: AsyncSession,
) -> dict:
    """
    Return a structured context status dict for the given user + model.

    Returns:
        {
          "model_id": str,
          "remaining_pct": int | None,
          "exhausted": bool,
          "message": str | None,   # set when exhausted
        }
    """
    pct = await get_remaining_pct(user_id, model_id, db)
    exhausted = pct == 0 if pct is not None else False
    return {
        "model_id": model_id,
        "remaining_pct": pct,
        "exhausted": exhausted,
        "message": (
            f"{model_id} Models context windows is used completely, "
            "switch to another model to use the application"
        )
        if exhausted
        else None,
    }


async def get_usage_analytics(
    user_id: str,
    db: AsyncSession,
) -> dict:
    """
    Aggregate usage statistics for the given user (T-BACK-02).

    Returns:
      total_tokens, tokens_by_model, daily_tokens, lines_written, sessions_count
    """
    from app.models.session import Session  # avoid circular import

    # Total tokens
    total_result = await db.execute(
        select(func.coalesce(func.sum(ModelUsage.tokens_used), 0)).where(
            ModelUsage.user_id == user_id
        )
    )
    total_tokens: int = total_result.scalar_one()

    # Tokens by model
    model_result = await db.execute(
        select(ModelUsage.model_id, func.sum(ModelUsage.tokens_used))
        .where(ModelUsage.user_id == user_id)
        .group_by(ModelUsage.model_id)
    )
    tokens_by_model: dict[str, int] = {
        row[0]: int(row[1]) for row in model_result.all()
    }

    # Daily tokens (last 30 days)
    from sqlalchemy import cast, Date as SQLDate  # noqa: PLC0415
    daily_result = await db.execute(
        select(
            cast(ModelUsage.created_at, SQLDate).label("day"),
            func.sum(ModelUsage.tokens_used).label("tokens"),
        )
        .where(ModelUsage.user_id == user_id)
        .group_by("day")
        .order_by("day")
    )
    daily_tokens = [
        {"date": str(row[0]), "tokens": int(row[1])}
        for row in daily_result.all()
    ]

    # Lines written (total)
    lines_result = await db.execute(
        select(func.coalesce(func.sum(ModelUsage.lines_written), 0)).where(
            ModelUsage.user_id == user_id
        )
    )
    lines_written: int = lines_result.scalar_one()

    # Daily lines written (for contribution heatmap)
    daily_lines_result = await db.execute(
        select(
            cast(ModelUsage.created_at, SQLDate).label("day"),
            func.sum(ModelUsage.lines_written).label("lines"),
        )
        .where(ModelUsage.user_id == user_id)
        .group_by("day")
        .order_by("day")
    )
    daily_lines_written = [
        {"date": str(row[0]), "lines": int(row[1] or 0)}
        for row in daily_lines_result.all()
    ]

    # Sessions count
    sessions_result = await db.execute(
        select(func.count(Session.id)).where(
            Session.user_id == user_id,
        )
    )
    sessions_count: int = sessions_result.scalar_one()

    return {
        "total_tokens": total_tokens,
        "tokens_by_model": tokens_by_model,
        "daily_tokens": daily_tokens,
        "daily_lines_written": daily_lines_written,
        "lines_written": lines_written,
        "sessions_count": sessions_count,
    }
