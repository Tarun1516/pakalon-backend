"""Notifications router — in-app notification management (T-BACK-NOTIFY)."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notifications import (
    NotificationCreateRequest,
    NotificationListResponse,
    NotificationReadResponse,
    NotificationResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get(
    "",
    response_model=NotificationListResponse,
    summary="List user's notifications (unread first, paginated)",
)
async def list_notifications(
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    unread_only: bool = Query(default=False, description="Return only unread notifications"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> NotificationListResponse:
    """
    Return paginated notifications for the authenticated user.

    Expired notifications (expires_at < now) are excluded automatically.
    Unread notifications are returned first, then sorted by created_at desc.
    """
    now = datetime.now(tz=timezone.utc)

    base_filter = [
        Notification.user_id == current_user.id,
        # Exclude notifications that have passed their TTL
        (Notification.expires_at == None) | (Notification.expires_at > now),  # noqa: E711
    ]
    if unread_only:
        base_filter.append(Notification.read == False)  # noqa: E712

    q = (
        select(Notification)
        .where(*base_filter)
        .order_by(Notification.read.asc(), Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    count_q = select(func.count()).select_from(Notification).where(*base_filter)
    unread_q = (
        select(func.count())
        .select_from(Notification)
        .where(
            Notification.user_id == current_user.id,
            Notification.read == False,  # noqa: E712
            (Notification.expires_at == None) | (Notification.expires_at > now),  # noqa: E711
        )
    )

    result = await session.execute(q)
    notifications = result.scalars().all()

    count_result = await session.execute(count_q)
    total = count_result.scalar_one()

    unread_result = await session.execute(unread_q)
    unread_count = unread_result.scalar_one()

    return NotificationListResponse(
        notifications=[NotificationResponse.model_validate(n) for n in notifications],
        total=total,
        unread_count=unread_count,
    )


@router.patch(
    "/{notification_id}/read",
    response_model=NotificationReadResponse,
    summary="Mark a single notification as read",
)
async def mark_notification_read(
    notification_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> NotificationReadResponse:
    """Mark one notification as read. Idempotent — safe to call multiple times."""
    result = await session.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == current_user.id,
        )
    )
    notif = result.scalar_one_or_none()
    if notif is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    notif.read = True
    await session.commit()
    await session.refresh(notif)
    return NotificationReadResponse(id=notif.id, read=notif.read)


@router.post(
    "/read-all",
    response_model=dict[str, int],
    summary="Mark all of the user's unread notifications as read",
)
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Bulk-mark every unread notification for the current user as read."""
    result = await session.execute(
        select(Notification).where(
            Notification.user_id == current_user.id,
            Notification.read == False,  # noqa: E712
        )
    )
    unread = result.scalars().all()
    for notif in unread:
        notif.read = True
    await session.commit()
    return {"marked_read": len(unread)}


@router.post(
    "",
    response_model=NotificationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a notification (internal use — no user auth required)",
    include_in_schema=False,  # Hide from public Swagger docs
)
async def create_notification(
    body: NotificationCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> NotificationResponse:
    """
    Internal endpoint for background jobs and services to create in-app notifications.

    Not authenticated — only reachable via localhost or internal network.
    Hidden from public API docs.
    """
    notif = Notification(
        id=str(uuid.uuid4()),
        user_id=body.user_id,
        notification_type=body.notification_type,
        title=body.title,
        body=body.body,
        action_url=body.action_url,
        action_label=body.action_label,
        expires_at=body.expires_at,
        read=False,
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(notif)
    await session.commit()
    await session.refresh(notif)
    logger.info(
        "Created notification type=%s user_id=%s id=%s",
        notif.notification_type,
        notif.user_id,
        notif.id,
    )
    return NotificationResponse.model_validate(notif)
