"""Pydantic schemas for automation workflows and OAuth connectors."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AutomationTemplateResponse(BaseModel):
    key: str
    name: str
    description: str
    recommended_connectors: list[str]
    default_cron: str
    prompt_hint: str


class AutomationCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    prompt: str = Field(..., min_length=5)
    required_connectors: list[str] | None = None
    schedule_cron: str | None = Field(default=None, max_length=100)
    schedule_timezone: str = Field(default="UTC", max_length=64)
    template_key: str | None = Field(default=None, max_length=100)


class AutomationUpdateRequest(BaseModel):
    enabled: bool | None = None
    schedule_cron: str | None = Field(default=None, max_length=100)
    schedule_timezone: str | None = Field(default=None, max_length=64)


class ConnectorToggleRequest(BaseModel):
    enabled: bool


class OAuthStartResponse(BaseModel):
    provider: str
    auth_url: str


class AutomationConnectorResponse(BaseModel):
    provider: str
    display_name: str
    category: str
    logo_domain: str | None = None
    logo_url: str | None = None
    oauth_supported: bool
    enabled: bool = False
    connected: bool = False
    connection_status: str = "available"
    account_label: str | None = None
    scopes: list[str] = []
    coming_soon: bool = False


class AutomationResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    prompt: str
    template_key: str | None = None
    inferred_config: dict[str, Any] = {}
    required_connectors: list[str] = []
    schedule_cron: str | None = None
    schedule_timezone: str = "UTC"
    enabled: bool
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    missing_connectors: list[str] = []

    model_config = {"from_attributes": True}


class AutomationListResponse(BaseModel):
    automations: list[AutomationResponse]
    templates: list[AutomationTemplateResponse]


class AutomationLogResponse(BaseModel):
    id: str
    automation_id: str
    trigger_type: str
    status: str
    summary: str | None = None
    details: dict[str, Any] = {}
    started_at: datetime
    completed_at: datetime | None = None

    model_config = {"from_attributes": True}


class AutomationLogsListResponse(BaseModel):
    logs: list[AutomationLogResponse]


class CronJobResponse(BaseModel):
    automation_id: str
    automation_name: str
    schedule_cron: str
    schedule_timezone: str
    enabled: bool
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None


class CronJobsListResponse(BaseModel):
    cron_jobs: list[CronJobResponse]


class ConnectorCatalogResponse(BaseModel):
    connected: list[AutomationConnectorResponse]
    available: list[AutomationConnectorResponse]


class AutomationRunResponse(BaseModel):
    queued: bool
    automation_id: str
    message: str