"""Pydantic schemas for webhook payloads (Polar)."""
from typing import Any

from pydantic import BaseModel


class WebhookResponse(BaseModel):
    status: str = "ok"
    message: str = ""


class PolarWebhookPayload(BaseModel):
    """Generic structure for incoming Polar webhook events."""
    type: str
    data: dict[str, Any] = {}
