"""Pydantic schemas for usage / analytics endpoints (T-BACK-02)."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class DailyTokens(BaseModel):
    """Token usage for a single day."""
    date: str
    tokens: int


class DailyLines(BaseModel):
    """Lines written for a single day (used by contribution heatmap)."""
    date: str
    lines: int


class ContributionDay(BaseModel):
    """Single day contribution data for heatmap visualization."""
    date: str
    lines_added: int = 0
    lines_deleted: int = 0
    commits: int = 0
    tokens_used: int = 0
    sessions_count: int = 0
    level: int = 0  # 0-4 intensity level for heatmap


class HeatmapResponse(BaseModel):
    """Response for GET /usage/heatmap — contribution data for a year."""
    year: int
    contributions: list[ContributionDay]
    total_lines_added: int = 0
    total_lines_deleted: int = 0
    total_commits: int = 0
    total_tokens: int = 0


class UsageResponse(BaseModel):
    """Response for GET /usage — trial status + full analytics."""
    user_id: str
    plan: str
    trial_days_used: int
    trial_days_remaining: int
    subscription_id: str | None = None
    subscription_status: str | None = None
    current_period_start: datetime | None = None  # prepaid cycle day-0
    current_period_end: datetime | None = None    # prepaid cycle day-30
    days_into_cycle: int | None = None            # how many days used of the 30-day period
    is_in_grace_period: bool = False
    grace_period_warning: bool = False   # in-app banner flag for grace period
    grace_days_remaining: int = 0        # days left in grace period
    # Analytics (T-BACK-02)
    total_tokens: int = 0
    tokens_by_model: dict[str, int] = {}
    daily_tokens: list[DailyTokens] = []
    daily_lines_written: list[DailyLines] = []  # for contribution heatmap
    lines_written: int = 0
    sessions_count: int = 0


class SessionPrompt(BaseModel):
    """A single prompt/message in a session with metadata."""
    timestamp: datetime
    prompt: str
    tokens_used: int
    role: Literal["user", "assistant"]


class SessionPromptsResponse(BaseModel):
    """Response for GET /sessions/{id}/prompts — prompts history."""
    session_id: str
    prompts: list[SessionPrompt]


class SessionPrompt(BaseModel):
    """A single prompt/message in a session with metadata."""
    timestamp: datetime
    prompt: str
    tokens_used: int
    role: Literal["user", "assistant"]


class SessionPromptsResponse(BaseModel):
    """Response for GET /sessions/{id}/prompts — prompts history."""
    session_id: str
    prompts: list[SessionPrompt]


class SessionPrompt(BaseModel):
    """A single prompt/message in a session with metadata."""
    timestamp: datetime
    prompt: str
    tokens_used: int
    role: str


class SessionPromptsResponse(BaseModel):
    """Response for GET /sessions/{id}/prompts — prompts history."""
    session_id: str
    prompts: list[SessionPrompt]
