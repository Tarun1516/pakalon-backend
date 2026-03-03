"""Pydantic schemas for auth endpoints."""

from pydantic import BaseModel, Field


class DeviceCodeCreateRequest(BaseModel):
    """Request to create a new device code."""
    device_id: str | None = Field(
        None,
        max_length=255,
        description="Stable per-machine identifier (generated server-side if omitted)",
    )
    machine_id: str | None = Field(None, max_length=512, description="Hashed machine fingerprint")


class DeviceCodeCreateResponse(BaseModel):
    """Response after creating a device code."""
    code: str = Field(..., description="6-digit numeric code to display to the user")
    device_id: str
    expires_in: int = Field(..., description="TTL in seconds")


class DeviceCodePollResponse(BaseModel):
    """Response from the polling endpoint."""
    status: str = Field(..., description="pending | approved | expired")
    token: str | None = Field(None, description="JWT — only present when status=approved")
    user_id: str | None = None
    plan: str | None = None
    trial_days_remaining: int | None = Field(
        None,
        description="Days left in free trial; None for pro/enterprise; 0 = expired",
    )
    trial_ends_at: str | None = Field(
        None,
        description="ISO-8601 date when trial ends (free accounts only)",
    )


class DeviceCodeConfirmRequest(BaseModel):
    """Request to confirm a device code (from website with Clerk session)."""
    code: str = Field(
        ...,
        pattern=r"^\d{6}$",
        description="6-digit numeric code shown in CLI",
    )


class DeviceCodeConfirmResponse(BaseModel):
    """Response after confirming a device code."""
    status: str = Field(..., description="approved")
    token: str = Field(..., description="JWT issued for CLI session")
    user_id: str
    plan: str
