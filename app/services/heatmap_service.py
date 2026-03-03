"""Heatmap service — contribution heatmap data aggregation."""
import logging
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contribution_heatmap import ContributionHeatmap
from app.models.model_usage import ModelUsage

logger = logging.getLogger(__name__)


def _calculate_level(total: int) -> int:
    """Calculate heatmap intensity level (0-4) based on contribution total."""
    if total == 0:
        return 0
    if total <= 5:
        return 1
    if total <= 15:
        return 2
    if total <= 30:
        return 3
    return 4


async def get_contribution_heatmap(
    user_id: str,
    year: int,
    db: AsyncSession,
) -> dict[str, Any]:
    """
    Get contribution heatmap data for a specific year.

    Returns contribution data for each day of the year, aggregated from:
    - ModelUsage (lines written, tokens used)
    - Sessions (session count)
    """
    # Get start and end of year
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)

    # Query contributions for the year
    result = await db.execute(
        select(ContributionHeatmap).where(
            ContributionHeatmap.user_id == user_id,
            ContributionHeatmap.contribution_date >= start_date,
            ContributionHeatmap.contribution_date <= end_date,
        )
    )
    contributions = result.scalars().all()

    # Create lookup dict
    contrib_by_date: dict[date, dict[str, Any]] = {}
    for c in contributions:
        contrib_by_date[c.contribution_date] = {
            "date": c.contribution_date.isoformat(),
            "lines_added": c.lines_added,
            "lines_deleted": c.lines_deleted,
            "commits": c.commits,
            "tokens_used": c.tokens_used,
            "sessions_count": c.sessions_count,
        }

    # Build full year with zeros for missing days
    from datetime import timedelta

    current = start_date
    all_days = []
    total_lines_added = 0
    total_lines_deleted = 0
    total_commits = 0
    total_tokens = 0

    while current <= end_date:
        day_data = contrib_by_date.get(current)
        if day_data:
            day = day_data
        else:
            day = {
                "date": current.isoformat(),
                "lines_added": 0,
                "lines_deleted": 0,
                "commits": 0,
                "tokens_used": 0,
                "sessions_count": 0,
            }

        # Calculate intensity level
        total = day["lines_added"] + day["commits"] + day["sessions_count"]
        day["level"] = _calculate_level(total)

        all_days.append(day)
        total_lines_added += day["lines_added"]
        total_lines_deleted += day["lines_deleted"]
        total_commits += day["commits"]
        total_tokens += day["tokens_used"]

        current += timedelta(days=1)

    return {
        "year": year,
        "contributions": all_days,
        "total_lines_added": total_lines_added,
        "total_lines_deleted": total_lines_deleted,
        "total_commits": total_commits,
        "total_tokens": total_tokens,
    }


async def update_contribution_day(
    user_id: str,
    db: AsyncSession,
    lines_added: int = 0,
    lines_deleted: int = 0,
    commits: int = 0,
    tokens_used: int = 0,
    sessions_count: int = 0,
) -> None:
    """
    Update contribution data for today.
    Called after sessions end or model usage is recorded.
    """
    today = date.today()

    # Check if entry exists
    result = await db.execute(
        select(ContributionHeatmap).where(
            ContributionHeatmap.user_id == user_id,
            ContributionHeatmap.contribution_date == today,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.lines_added += lines_added
        existing.lines_deleted += lines_deleted
        existing.commits += commits
        existing.tokens_used += tokens_used
        existing.sessions_count += sessions_count
        existing.updated_at = datetime.now(tz=timezone.utc)
    else:
        new_contrib = ContributionHeatmap(
            user_id=user_id,
            contribution_date=today,
            lines_added=lines_added,
            lines_deleted=lines_deleted,
            commits=commits,
            tokens_used=tokens_used,
            sessions_count=sessions_count,
        )
        db.add(new_contrib)

    await db.commit()
