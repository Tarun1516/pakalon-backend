"""Pydantic schemas for in-app notifications."""

from datetime import datetime

from pydantic import BaseModel


class NotificationResponse(BaseModel):
    id: str
    user_id: str
    notification_type: str
    title: str
    body: str
    action_url: str | None = None
    action_label: str | None = None
    read: bool
    created_at: datetime
    expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class NotificationListResponse(BaseModel):
    notifications: list[NotificationResponse]
    total: int
    unread_count: int


class NotificationCreateRequest(BaseModel):
    """Internal-use payload for creating an in-app notification programmatically."""

    user_id: str
    notification_type: (
        str  # billing_reminder | trial_expiry | context_exhausted | plan_upgrade | grace_period
    )
    title: str
    body: str
    action_url: str | None = None
    action_label: str | None = None
    expires_at: datetime | None = None


class NotificationReadResponse(BaseModel):
    id: str
    read: bool
