"""Pydantic schemas for user endpoints."""
from datetime import datetime

from pydantic import BaseModel, Field


class MeResponse(BaseModel):
    """Response for GET /auth/me — flat user profile."""
    id: str
    github_login: str
    email: str
    display_name: str
    plan: str
    trial_days_used: int
    trial_days_remaining: int
    privacy_mode: bool = False
    figma_pat: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdateRequest(BaseModel):
    """Fields the user can update."""
    display_name: str | None = Field(None, max_length=255)
    privacy_mode: bool | None = None


class FigmaPatRequest(BaseModel):
    """Request body for storing/updating the Figma Personal Access Token."""
    pat: str = Field(..., min_length=10, max_length=512, description="Figma Personal Access Token")


class TelemetryResetRequest(BaseModel):
    """Request body for fake-pakalon reset endpoint (development only)."""
    reset_trial_days: bool = False


class TelemetryResetResponse(BaseModel):
    """Response summary for fake-pakalon reset endpoint."""
    user_id: str
    telemetry_deleted: int
    machine_ids_deleted: int
    heatmap_deleted: int
    trial_days_reset: bool
