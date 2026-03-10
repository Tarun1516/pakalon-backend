"""Automation workflows, connectors, cron jobs, and logs."""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.automation import Automation
from app.models.automation_connector import AutomationConnector
from app.models.automation_log import AutomationLog
from app.models.user import User
from app.schemas.automations import (
    AutomationCreateRequest,
    AutomationListResponse,
    AutomationLogsListResponse,
    AutomationResponse,
    AutomationRunResponse,
    AutomationUpdateRequest,
    ConnectorCatalogResponse,
    ConnectorToggleRequest,
    CronJobResponse,
    CronJobsListResponse,
    OAuthStartResponse,
)
from app.services import automations as automation_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/automations", tags=["automations"])


def _get_redis():
    from app.main import redis_client  # noqa: PLC0415

    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis is not available")
    return redis_client


async def _get_owned_automation(automation_id: str, current_user: User, session: AsyncSession) -> Automation:
    automation = await session.get(Automation, automation_id)
    if automation is None or automation.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Automation not found")
    return automation


@router.get("", response_model=AutomationListResponse, summary="List automations and starter templates")
async def list_automations(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AutomationListResponse:
    automations = await automation_svc.list_automations_for_user(current_user.id, session)
    connectors = await automation_svc.list_connectors_for_user(current_user.id, session)
    connected_providers = {connector.provider for connector in connectors if connector.enabled}
    return AutomationListResponse(
        automations=[
            AutomationResponse(
                **AutomationResponse.model_validate(automation).model_dump(),
                missing_connectors=[
                    provider for provider in (automation.required_connectors or []) if provider not in connected_providers
                ],
            )
            for automation in automations
        ],
        templates=automation_svc.get_templates(),
    )


@router.post("", response_model=AutomationResponse, status_code=status.HTTP_201_CREATED, summary="Create a new automation workflow")
async def create_automation(
    body: AutomationCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    automation = await automation_svc.create_automation(
        user_id=current_user.id,
        name=body.name,
        prompt=body.prompt,
        required_connectors_override=body.required_connectors,
        schedule_cron=body.schedule_cron,
        schedule_timezone=body.schedule_timezone,
        template_key=body.template_key,
        session=session,
    )
    connectors = await automation_svc.list_connectors_for_user(current_user.id, session)
    connected_providers = {connector.provider for connector in connectors if connector.enabled}
    return AutomationResponse(
        **AutomationResponse.model_validate(automation).model_dump(),
        missing_connectors=[provider for provider in (automation.required_connectors or []) if provider not in connected_providers],
    )


@router.patch("/{automation_id}", response_model=AutomationResponse, summary="Update automation enabled state or schedule")
async def update_automation(
    automation_id: str,
    body: AutomationUpdateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AutomationResponse:
    automation = await _get_owned_automation(automation_id, current_user, session)
    await automation_svc.update_automation(
        automation,
        enabled=body.enabled,
        schedule_cron=body.schedule_cron,
        schedule_timezone=body.schedule_timezone,
    )
    return AutomationResponse.model_validate(automation)


@router.delete("/{automation_id}", response_model=AutomationRunResponse, summary="Delete an automation")
async def delete_automation(
    automation_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    automation = await _get_owned_automation(automation_id, current_user, session)
    await automation_svc.delete_automation(automation, session)
    return AutomationRunResponse(queued=True, automation_id=automation_id, message="Automation deleted")


@router.post("/{automation_id}/run", response_model=AutomationRunResponse, summary="Run an automation immediately")
async def run_automation_now(
    automation_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AutomationRunResponse:
    automation = await _get_owned_automation(automation_id, current_user, session)
    await automation_svc.run_automation_job(automation.id, trigger_type="manual")
    return AutomationRunResponse(queued=True, automation_id=automation.id, message="Automation run completed")


@router.get("/connectors", response_model=ConnectorCatalogResponse, summary="List connected apps and available connector catalog")
async def list_connectors(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConnectorCatalogResponse:
    payload = await automation_svc.build_connector_view(current_user.id, session)
    return ConnectorCatalogResponse(**payload)


@router.post("/connectors/{provider}/oauth/start", response_model=OAuthStartResponse, summary="Start OAuth flow for a connector")
async def start_connector_oauth(
    provider: str,
    current_user: User = Depends(get_current_user),
    _: AsyncSession = Depends(get_session),
) -> OAuthStartResponse:
    try:
        meta = automation_svc._get_provider_meta(provider)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not meta["oauth_supported"]:
        raise HTTPException(status_code=400, detail=f"OAuth is not yet available for {provider}")

    redis = _get_redis()
    state_token = uuid.uuid4().hex
    await redis.setex(
        f"automation_oauth:{state_token}",
        600,
        json.dumps({"user_id": current_user.id, "provider": provider}),
    )
    auth_url = await automation_svc.build_oauth_url(provider, current_user.id, state_token)
    return OAuthStartResponse(provider=provider, auth_url=auth_url)


@router.post("/connectors/{provider}/toggle", response_model=ConnectorCatalogResponse, summary="Enable or disable a connector")
async def toggle_connector(
    provider: str,
    body: ConnectorToggleRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConnectorCatalogResponse:
    row = await session.execute(
        select(AutomationConnector).where(
            AutomationConnector.user_id == current_user.id,
            AutomationConnector.provider == provider,
        )
    )
    connector = row.scalar_one_or_none()
    if connector is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    await automation_svc.toggle_connector(connector, body.enabled)
    payload = await automation_svc.build_connector_view(current_user.id, session)
    return ConnectorCatalogResponse(**payload)


@router.get("/oauth/{provider}/callback", include_in_schema=False)
async def connector_oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    session: AsyncSession = Depends(get_session),
):
    redis = _get_redis()
    key = f"automation_oauth:{state}"
    raw = await redis.get(key)
    if raw is None:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    payload = json.loads(raw)
    await redis.delete(key)

    if payload.get("provider") != provider:
        raise HTTPException(status_code=400, detail="OAuth provider mismatch")

    token_payload = await automation_svc.exchange_oauth_code(provider, code)
    await automation_svc.upsert_connector(
        user_id=payload["user_id"],
        provider=provider,
        token_payload=token_payload,
        session=session,
    )

    from app.config import get_settings  # noqa: PLC0415

    redirect_url = (
        f"{get_settings().frontend_url.rstrip('/')}/dashboard/automations/connectors"
        f"?oauth=success&provider={provider}"
    )
    return RedirectResponse(url=redirect_url, status_code=302)


@router.get("/cron-jobs", response_model=CronJobsListResponse, summary="List automation cron jobs")
async def list_cron_jobs(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> CronJobsListResponse:
    automations = await automation_svc.list_automations_for_user(current_user.id, session)
    cron_jobs = []
    for automation in automations:
        if not automation.schedule_cron:
            continue
        job = automation_svc.scheduler.get_job(f"automation:{automation.id}")
        cron_jobs.append(
            CronJobResponse(
                automation_id=automation.id,
                automation_name=automation.name,
                schedule_cron=automation.schedule_cron,
                schedule_timezone=automation.schedule_timezone,
                enabled=automation.enabled,
                next_run_at=job.next_run_time if job else None,
                last_run_at=automation.last_run_at,
                last_status=automation.last_status,
            )
        )
    return CronJobsListResponse(cron_jobs=cron_jobs)


@router.get("/logs", response_model=AutomationLogsListResponse, summary="List automation execution logs")
async def list_automation_logs(
    automation_id: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AutomationLogsListResponse:
    query = select(AutomationLog).where(AutomationLog.user_id == current_user.id)
    if automation_id:
        query = query.where(AutomationLog.automation_id == automation_id)
    query = query.order_by(AutomationLog.started_at.desc()).limit(200)
    rows = await session.execute(query)
    return AutomationLogsListResponse(logs=list(rows.scalars()))