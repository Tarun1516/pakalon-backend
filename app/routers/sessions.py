"""Sessions router — chat history sync (T043)."""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.session import Session
from app.models.message import Message
from app.models.user import User
from app.models.model_usage import ModelUsage
from app.schemas.sessions import (
    SessionCreateRequest,
    SessionContextUpdateRequest,
    SessionResponse,
    SessionListResponse,
    MessageCreateRequest,
    MessageResponse,
    MessageListResponse,
    UsageRecordRequest,
    UsageRecordResponse,
)
from app.schemas.usage import SessionPromptsResponse, SessionPrompt
from app.services.usage_analytics import record_model_usage, is_context_exhausted

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new chat session",
)
async def create_session(
    body: SessionCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # T-BACK-06 / T-BACK-09: Block new sessions when context window exhausted
    if body.model_id:
        exhausted = await is_context_exhausted(current_user.id, body.model_id, session)
        if exhausted:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Context window for model '{body.model_id}' is exhausted "
                    "(0% remaining). Start a new session or switch to a different model."
                ),
                headers={"X-Pakalon-Context-Exhausted": "true"},
            )
    new_session = Session(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        title=body.title or "New Chat",
        model_id=body.model_id,
        mode=body.mode or "chat",
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(new_session)
    await session.commit()
    await session.refresh(new_session)
    return SessionResponse.model_validate(new_session)


@router.get(
    "",
    response_model=SessionListResponse,
    summary="List user's sessions (optionally filtered by project directory)",
)
async def list_sessions(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    project_dir: str | None = Query(default=None, description="Filter sessions by project directory path"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    q = select(Session).where(Session.user_id == current_user.id)
    count_q = select(func.count()).select_from(Session).where(Session.user_id == current_user.id)

    # Per-directory session filtering — only return sessions for this project path
    if project_dir:
        q = q.where(Session.project_dir == project_dir)
        count_q = count_q.where(Session.project_dir == project_dir)

    result = await session.execute(
        q.order_by(Session.created_at.desc()).limit(limit).offset(offset)
    )
    sessions = result.scalars().all()
    count_result = await session.execute(count_q)
    total = count_result.scalar_one()

    # Aggregate per-session stats in one query each
    session_ids = [s.id for s in sessions]
    msg_counts: dict[str, int] = {}
    token_sums: dict[str, int] = {}
    lines_sums: dict[str, int] = {}
    if session_ids:
        msg_result = await session.execute(
            select(Message.session_id, func.count(Message.id))
            .where(Message.session_id.in_(session_ids))
            .group_by(Message.session_id)
        )
        msg_counts = {row[0]: row[1] for row in msg_result.all()}

        usage_result = await session.execute(
            select(
                ModelUsage.session_id,
                func.sum(ModelUsage.tokens_used),
                func.sum(ModelUsage.lines_written),
            )
            .where(ModelUsage.session_id.in_(session_ids))
            .group_by(ModelUsage.session_id)
        )
        for row in usage_result.all():
            token_sums[row[0]] = int(row[1] or 0)
            lines_sums[row[0]] = int(row[2] or 0)

    # Fetch the first user prompt text per session (for history preview)
    first_prompts: dict[str, str] = {}
    if session_ids:
        # Subquery: earliest created_at per session_id for user messages
        first_msg_subq = (
            select(
                Message.session_id,
                func.min(Message.created_at).label("min_created_at"),
            )
            .where(
                and_(
                    Message.session_id.in_(session_ids),
                    Message.role == "user",
                )
            )
            .group_by(Message.session_id)
            .subquery()
        )
        first_msg_result = await session.execute(
            select(Message.session_id, Message.content)
            .join(
                first_msg_subq,
                and_(
                    Message.session_id == first_msg_subq.c.session_id,
                    Message.created_at == first_msg_subq.c.min_created_at,
                ),
            )
        )
        for row in first_msg_result.all():
            text = str(row[1] or "").strip().replace("\n", " ")
            first_prompts[row[0]] = text[:200] if len(text) > 200 else text

    enriched = []
    for s in sessions:
        resp = SessionResponse.model_validate(s)
        resp.messages_count = msg_counts.get(s.id, 0)
        resp.tokens_used = token_sums.get(s.id, 0)
        resp.lines_written = lines_sums.get(s.id, 0)
        resp.prompt_text = first_prompts.get(s.id)
        enriched.append(resp)

    return SessionListResponse(sessions=enriched, total=total)


@router.get(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Get a single session",
)
async def get_session_by_id(
    session_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == current_user.id,
        )
    )
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Aggregate stats for this session
    resp = SessionResponse.model_validate(sess)
    msg_count_result = await session.execute(
        select(func.count(Message.id)).where(Message.session_id == session_id)
    )
    resp.messages_count = msg_count_result.scalar_one() or 0

    usage_result = await session.execute(
        select(func.sum(ModelUsage.tokens_used), func.sum(ModelUsage.lines_written))
        .where(ModelUsage.session_id == session_id)
    )
    usage_row = usage_result.one_or_none()
    resp.tokens_used = int(usage_row[0] or 0) if usage_row else 0
    resp.lines_written = int(usage_row[1] or 0) if usage_row else 0

    # Fetch first user prompt text preview
    first_msg_result = await session.execute(
        select(Message.content)
        .where(
            and_(
                Message.session_id == session_id,
                Message.role == "user",
            )
        )
        .order_by(Message.created_at.asc())
        .limit(1)
    )
    first_msg_row = first_msg_result.scalar_one_or_none()
    if first_msg_row:
        text = str(first_msg_row).strip().replace("\n", " ")
        resp.prompt_text = text[:200] if len(text) > 200 else text

    return resp


@router.patch(
    "/{session_id}",
    response_model=SessionResponse,
    summary="Update session metadata (e.g. context_pct_used)",
)
async def update_session(
    session_id: str,
    body: SessionContextUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == current_user.id,
        )
    )
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    sess.context_pct_used = round(max(0.0, min(100.0, body.context_pct_used)), 2)
    # Accumulate code change lineage — add the incremental delta sent by the CLI
    if body.lines_added is not None:
        sess.lines_added = (sess.lines_added or 0) + body.lines_added
    if body.lines_deleted is not None:
        sess.lines_deleted = (sess.lines_deleted or 0) + body.lines_deleted
    await session.commit()
    await session.refresh(sess)
    return SessionResponse.model_validate(sess)


@router.post(
    "/{session_id}/messages",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append a message to a session",
)
async def create_message(
    session_id: str,
    body: MessageCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # Verify session ownership
    result = await session.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == current_user.id,
        )
    )
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # T-BACK-06 / T-BACK-09: Block new assistant messages when context window is exhausted.
    # Only check for assistant (AI-generated) messages as a guard before AI calls.
    if body.role == "user" and sess.model_id:
        exhausted = await is_context_exhausted(current_user.id, sess.model_id, session)
        if exhausted:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Context window for model '{sess.model_id}' is exhausted "
                    "(0% remaining). Start a new session or switch to a different model."
                ),
                headers={"X-Pakalon-Context-Exhausted": "true"},
            )

    new_msg = Message(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role=body.role,
        content=body.content,
        tool_calls=body.tool_calls,
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(new_msg)
    await session.commit()
    await session.refresh(new_msg)
    return MessageResponse.model_validate(new_msg)


@router.get(
    "/{session_id}/messages",
    response_model=MessageListResponse,
    summary="List messages in a session",
)
async def list_messages(
    session_id: str,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == current_user.id,
        )
    )
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    msg_result = await session.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    messages = msg_result.scalars().all()
    count_result = await session.execute(
        select(func.count()).select_from(Message).where(Message.session_id == session_id)
    )
    total = count_result.scalar_one()
    return MessageListResponse(
        messages=[MessageResponse.model_validate(m) for m in messages],
        total=total,
    )


@router.post(
    "/{session_id}/usage",
    response_model=UsageRecordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record AI token usage and check context window (T-BACK-01, T-BACK-06)",
)
async def record_usage(
    session_id: str,
    body: UsageRecordRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Record token usage for one AI inference call within a session.

    If the context window is fully consumed, returns HTTP 422 with a human-
    friendly message instructing the user to switch models (T-BACK-06).
    """
    # Verify session ownership
    result = await session.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == current_user.id,
        )
    )
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Session not found"
        )

    # T-BACK-06: check context exhaustion before recording
    if (
        body.context_window_size > 0
        and body.context_window_used >= body.context_window_size
    ):
        # Fetch model display name from cache if possible
        from app.models.model_cache import ModelCache  # noqa: PLC0415
        from app.services.model_registry import ensure_model_cache_schema_compat  # noqa: PLC0415

        await ensure_model_cache_schema_compat(session)
        mc_result = await session.execute(
            select(ModelCache).where(ModelCache.model_id == body.model_id)
        )
        mc = mc_result.scalar_one_or_none()
        display_name = mc.name if mc else body.model_id
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"{display_name} Models context window is used completely, "
                "switch to another model"
            ),
        )

    record = await record_model_usage(
        user_id=current_user.id,
        model_id=body.model_id,
        tokens_used=body.tokens_used,
        context_window_size=body.context_window_size,
        context_window_used=body.context_window_used,
        lines_written=body.lines_written,
        session_id=session_id,
        db=session,
    )
    await session.commit()

    remaining_pct: int | None = None
    if body.context_window_size > 0:
        remaining_pct = max(
            0,
            100 - round(body.context_window_used / body.context_window_size * 100),
        )

    return UsageRecordResponse(recorded=True, remaining_pct=remaining_pct)


@router.get(
    "/{session_id}/prompts",
    response_model=SessionPromptsResponse,
    summary="Get session prompts history with tokens used",
)
async def get_session_prompts(
    session_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Returns all prompts in a session with timestamps and token usage.
    Useful for tracking conversation history and usage analytics.
    """
    # Verify session ownership
    result = await session.execute(
        select(Session).where(
            Session.id == session_id,
            Session.user_id == current_user.id,
        )
    )
    sess = result.scalar_one_or_none()
    if sess is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Get messages for the session
    msg_result = await session.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
    )
    messages = msg_result.scalars().all()

    # Get usage records for the session
    usage_result = await session.execute(
        select(ModelUsage)
        .where(ModelUsage.session_id == session_id)
        .order_by(ModelUsage.created_at.asc())
    )
    usage_records = usage_result.scalars().all()

    # Build usage lookup by timestamp (roughly)
    usage_by_msg_index: dict[int, int] = {}
    for i, record in enumerate(usage_records):
        usage_by_msg_index[i] = record.tokens_used

    # Build prompts list - user messages with their tokens
    prompts = []
    user_msg_count = 0
    for i, msg in enumerate(messages):
        if msg.role == "user":
            user_msg_count += 1
            tokens = usage_by_msg_index.get(user_msg_count - 1, 0)
            prompts.append(SessionPrompt(
                timestamp=msg.created_at,
                prompt=msg.content[:500],  # Limit prompt length
                tokens_used=tokens,
                role="user",
            ))
        elif msg.role == "assistant" and msg.content:
            tokens = usage_by_msg_index.get(user_msg_count, 0)
            prompts.append(SessionPrompt(
                timestamp=msg.created_at,
                prompt=msg.content[:500],
                tokens_used=tokens,
                role="assistant",
            ))

    return SessionPromptsResponse(session_id=session_id, prompts=prompts)

