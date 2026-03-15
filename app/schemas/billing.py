"""Pydantic schemas for billing endpoints."""
from datetime import datetime

from pydantic import BaseModel


class CheckoutRequest(BaseModel):
    success_url: str | None = None


class CheckoutResponse(BaseModel):
    checkout_url: str
    expires_at: datetime | None = None


class SubscriptionStatusResponse(BaseModel):
    status: str  # active | canceled | past_due | none
    polar_sub_id: str | None = None
    period_start: datetime | None = None
    current_period_end: datetime | None = None
    grace_until: datetime | None = None
    plan: str | None = None
    days_remaining: int | None = None  # T-BE-08: Days left in billing cycle
    in_grace_period: bool = False  # T-BE-09: User is in 3-day grace period
    days_into_cycle: int | None = None
    billing_model: str | None = None
    security_deposit_usd: float | None = None
    platform_fee_rate: float | None = None
    usage_charges_usd: float | None = None
    platform_fee_usd: float | None = None
    deposit_applied_usd: float | None = None
    estimated_total_due_usd: float | None = None
    cycle_token_usage: int | None = None
    usage_by_model: list[dict] = []


class SubscriptionResponse(BaseModel):
    status: str  # active | canceled | past_due | paused | free
    period_start: datetime | None
    period_end: datetime | None
    grace_end: datetime | None
    amount_usd: float | None
    payment_method: str | None  # e.g. "Visa ****4242"
    days_remaining: int | None = None
    in_grace_period: bool = False
    is_active: bool = False


class CancelResponse(BaseModel):
    status: str
    message: str


class PortalUrlResponse(BaseModel):
    """Polar customer portal URL for updating payment details."""
    portal_url: str
