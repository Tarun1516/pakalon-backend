"""Pydantic schemas for sessions and messages."""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SessionCreateRequest(BaseModel):
    title: str | None = None
    model_id: str | None = None
    mode: str | None = "chat"  # chat | agent | headless


class SessionResponse(BaseModel):
    id: str
    user_id: str
    title: str | None = None
    model_id: str | None = None
    mode: str | None = None
    created_at: datetime
    # Per-session aggregate stats (populated by list/get endpoints)
    messages_count: int | None = None
    tokens_used: int | None = None
    lines_written: int | None = None
    # Live context-window utilisation synced from CLI after each AI step
    context_pct_used: float | None = None
    # Per-session code change lineage (lines added/deleted across all edits)
    lines_added: int = 0
    lines_deleted: int = 0
    # First user prompt text preview (truncated to 200 chars)
    prompt_text: str | None = None

    model_config = {"from_attributes": True}


class SessionContextUpdateRequest(BaseModel):
    """Lightweight PATCH payload for updating context window utilisation and code change lineage."""
    context_pct_used: float  # 0.0 – 100.0
    # Optional lineage counters — CLI sends these after each agentic edit step
    lines_added: int | None = None   # lines inserted in this update batch
    lines_deleted: int | None = None  # lines removed in this update batch

class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]
    total: int


class MessageCreateRequest(BaseModel):
    role: str  # user | assistant | system | tool
    content: str
    tool_calls: Any | None = None


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    tool_calls: Any | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageListResponse(BaseModel):
    messages: list[MessageResponse]
    total: int


class UsageRecordRequest(BaseModel):
    """Record token usage for an AI call within a session (T-BACK-01, T-BACK-06)."""
    model_id: str
    tokens_used: int
    context_window_size: int
    context_window_used: int
    lines_written: int = 0


class UsageRecordResponse(BaseModel):
    recorded: bool
    remaining_pct: int | None = None
