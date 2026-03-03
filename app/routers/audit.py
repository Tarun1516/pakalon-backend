"""
audit.py — Governance audit-trail API.

Endpoints
---------
  POST /audit/events             — record an audit event (called internally or by other routers)
  GET  /audit/events             — list audit events for the authenticated user (paginated)
  GET  /audit/export             — download full audit trail as CSV or JSON
  GET  /audit/admin/events       — (admin) list audit events for all users
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.dependencies import get_current_user
from app.models.audit_log import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/audit", tags=["audit"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AuditEventIn(BaseModel):
    """Payload accepted by POST /audit/events."""

    action: str
    resource_type: str = ""
    resource_id: Optional[str] = None
    extra: Optional[dict[str, Any]] = None


class AuditEventOut(BaseModel):
    """Response shape for a single audit event."""

    id: str
    user_id: Optional[str]
    action: str
    resource_type: str
    resource_id: Optional[str]
    ip_address: Optional[str]
    extra: Optional[dict[str, Any]]
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Admin guard (reuses ADMIN_API_KEY from settings — same as admin.py)
# ---------------------------------------------------------------------------


async def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """Verify static admin API key for /audit/admin/* routes."""
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API key not configured",
        )
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin API key",
        )


# ---------------------------------------------------------------------------
# Helper: extract client IP from request
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    """Return best-effort client IP (handles X-Forwarded-For)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/events",
    status_code=status.HTTP_201_CREATED,
    response_model=AuditEventOut,
    summary="Record an audit event",
)
async def create_audit_event(
    body: AuditEventIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AuditLog:
    """
    Record a structured audit event for the authenticated user.

    Called by other routers (e.g. billing, auth) to ensure every
    user-initiated action is persisted to the audit trail.
    """
    event = AuditLog(
        user_id=current_user.id,
        action=body.action,
        resource_type=body.resource_type,
        resource_id=body.resource_id,
        ip_address=_client_ip(request),
        extra=body.extra,
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


@router.get(
    "/events",
    response_model=dict,
    summary="List audit events for authenticated user",
)
async def list_audit_events(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    action: Optional[str] = Query(default=None, description="Filter by action prefix"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """
    Return a paginated list of audit events for the current user,
    newest first.
    """
    q = (
        select(AuditLog)
        .where(AuditLog.user_id == current_user.id)
        .order_by(AuditLog.created_at.desc())
    )
    if action:
        q = q.where(AuditLog.action.startswith(action))

    total_q = select(func.count()).select_from(q.with_only_columns(AuditLog.id).subquery())
    total: int = (await session.execute(total_q)).scalar_one()

    result = await session.execute(q.offset(offset).limit(limit))
    events = result.scalars().all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [AuditEventOut.model_validate(e).model_dump() for e in events],
    }


@router.get(
    "/export",
    summary="Export audit trail (CSV or JSON)",
)
async def export_audit_events(
    fmt: str = Query(default="json", pattern="^(json|csv)$"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """
    Download the full audit trail for the authenticated user.

    - ``?fmt=json`` (default) — newline-delimited JSON
    - ``?fmt=csv`` — RFC 4180 CSV with header row
    """
    result = await session.execute(
        select(AuditLog)
        .where(AuditLog.user_id == current_user.id)
        .order_by(AuditLog.created_at.asc())
    )
    events = result.scalars().all()

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(
            buf,
            fieldnames=[
                "id",
                "user_id",
                "action",
                "resource_type",
                "resource_id",
                "ip_address",
                "extra",
                "created_at",
            ],
        )
        writer.writeheader()
        for e in events:
            writer.writerow(
                {
                    "id": e.id,
                    "user_id": e.user_id,
                    "action": e.action,
                    "resource_type": e.resource_type,
                    "resource_id": e.resource_id or "",
                    "ip_address": e.ip_address or "",
                    "extra": json.dumps(e.extra) if e.extra else "",
                    "created_at": e.created_at.isoformat(),
                }
            )
        buf.seek(0)
        filename = f"audit_{current_user.id}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d')}.csv"
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # JSON (newline-delimited)
    lines = [json.dumps(AuditEventOut.model_validate(e).model_dump(mode="json")) for e in events]
    payload = "\n".join(lines) + "\n"
    filename = f"audit_{current_user.id}_{datetime.now(tz=timezone.utc).strftime('%Y%m%d')}.jsonl"
    return StreamingResponse(
        iter([payload]),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/admin/events",
    response_model=dict,
    summary="(Admin) List audit events across all users",
    dependencies=[Depends(require_admin)],
)
async def admin_list_audit_events(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user_id: Optional[str] = Query(default=None),
    action: Optional[str] = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Admin: list all audit events, optionally filtered by user or action."""
    q = select(AuditLog).order_by(AuditLog.created_at.desc())
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    if action:
        q = q.where(AuditLog.action.startswith(action))

    total_q = select(func.count()).select_from(q.with_only_columns(AuditLog.id).subquery())
    total: int = (await session.execute(total_q)).scalar_one()

    result = await session.execute(q.offset(offset).limit(limit))
    events = result.scalars().all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [AuditEventOut.model_validate(e).model_dump() for e in events],
    }
