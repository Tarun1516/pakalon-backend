"""Pydantic schemas for model registry endpoints."""
from pydantic import BaseModel


class ModelResponse(BaseModel):
    model_id: str
    name: str
    provider: str
    context_window: int
    pricing_tier: str  # free | pro
    supports_tools: bool


class ModelListResponse(BaseModel):
    models: list[ModelResponse]
    total: int
    plan: str
