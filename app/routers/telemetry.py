"""Telemetry router — anonymous event ingestion with IP geolocation (T044)."""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.models.telemetry_event import TelemetryEvent
from app.schemas.telemetry import TelemetryEventRequest, TelemetryEventResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/telemetry", tags=["telemetry"])


def _get_client_ip(request: Request) -> str:
    """Extract real client IP from request headers (respects proxies)."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take first IP (original client) from comma-separated list
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return ""


def _geolocate_ip_maxmind(ip: str) -> dict | None:
    """
    Resolve IP → geo data via local MaxMind GeoLite2 DB.
    Returns dict on success, None if DB not configured or lookup fails.
    """
    try:
        import geoip2.database  # noqa: PLC0415
        from app.config import get_settings  # noqa: PLC0415
        settings = get_settings()
        db_path = getattr(settings, "geoip_db_path", "")
        if not db_path:
            return None
        with geoip2.database.Reader(db_path) as reader:
            response = reader.city(ip)
            return {
                "country_code": response.country.iso_code or "",
                "country_name": response.country.name or "",
                "city": response.city.name or "",
                "latitude": response.location.latitude,
                "longitude": response.location.longitude,
                "timezone": response.location.time_zone or "",
            }
    except Exception:
        return None


def _geolocate_ip_api_com(ip: str) -> dict | None:
    """
    Resolve IP → geo data via ip-api.com (free, no key, ~45 req/min).
    Returns dict on success, None on any error.
    Docs: https://ip-api.com/docs/api:json
    """
    try:
        resp = httpx.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,countryCode,city,lat,lon,timezone"},
            timeout=5,
            headers={"User-Agent": "pakalon-backend/1.0"},
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return {
                    "country_code": data.get("countryCode", ""),
                    "country_name": data.get("country", ""),
                    "city": data.get("city", ""),
                    "latitude": data.get("lat"),
                    "longitude": data.get("lon"),
                    "timezone": data.get("timezone", ""),
                }
    except Exception as exc:
        logger.debug("ip-api.com lookup failed for %s: %s", ip, exc)
    return None


def _geolocate_ip(ip: str) -> dict:
    """
    Resolve IP → country/city.

    Strategy:
    1. MaxMind GeoLite2 local DB (primary — no external calls, fastest, most accurate).
    2. ip-api.com free API (fallback — requires outbound HTTP, rate-limited to 45 req/min).
    3. Return empty dict if both fail.

    Returns dict with: country_code, country_name, city, latitude, longitude, timezone.
    """
    if not ip or ip in ("127.0.0.1", "::1", "localhost"):
        return {}

    # Primary: local MaxMind DB
    result = _geolocate_ip_maxmind(ip)
    if result is not None:
        return result

    # Fallback: ip-api.com (free tier, no API key required)
    result = _geolocate_ip_api_com(ip)
    if result is not None:
        return result

    return {}


@router.post(
    "",
    response_model=TelemetryEventResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest an anonymous telemetry event",
)
async def ingest_event(
    body: TelemetryEventRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Accepts anonymous usage events from the CLI.

    No auth required — events that cannot be associated with a user are stored
    with user_id=NULL. IP address is resolved for geo-location data.
    Privacy Mode: if the user has privacy_mode=True the IP and geo data are
    not stored (checked via the optional JWT).
    """
    # Optional: extract user_id from a bearer token without requiring auth
    user_id: str | None = None
    privacy_mode: bool = False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            from app.middleware.auth import verify_pakalon_jwt  # noqa: PLC0415
            jwt_payload = verify_pakalon_jwt(auth_header[7:])
            user_id = jwt_payload.get("sub")
            privacy_mode = bool(jwt_payload.get("privacy_mode", False))
        except Exception:
            pass  # anonymous event is fine

    # T-BE-24: DB-authoritative privacy mode — DB value overrides JWT claim
    if user_id:
        try:
            from sqlalchemy import select  # noqa: PLC0415
            from app.models.user import User  # noqa: PLC0415
            pm_result = await session.execute(
                select(User.privacy_mode).where(User.id == user_id)
            )
            db_pm = pm_result.scalar_one_or_none()
            if db_pm is not None:
                privacy_mode = bool(db_pm)
        except Exception:
            pass  # fall back to JWT claim on DB error

    # IP geolocation (skipped if privacy mode enabled)
    client_ip = _get_client_ip(request)
    geo_data: dict = {}
    stored_ip: Optional[str] = None
    if not privacy_mode:
        stored_ip = client_ip
        geo_data = _geolocate_ip(client_ip)

    # Merge geo_data into properties — use sanitized_properties() to strip sensitive keys
    props = body.sanitized_properties()
    if geo_data:
        props["geo"] = geo_data
    if stored_ip and not privacy_mode:
        props["client_ip"] = stored_ip

    is_known = body.is_known_event()

    event = TelemetryEvent(
        id=str(uuid.uuid4()),
        user_id=user_id,
        event_name=body.event_name,
        properties=props,
        cli_version=body.cli_version,
        os_name=body.os_name,
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(event)
    await session.commit()
    return TelemetryEventResponse(id=event.id, recorded=True, known_event=is_known)


@router.get(
    "/geo-test",
    summary="Test GeoIP resolution for a given IP (dev only)",
    include_in_schema=False,
)
async def geo_test(ip: str = "8.8.8.8"):
    """Dev endpoint to verify GeoIP database is loading correctly."""
    return {"ip": ip, "geo": _geolocate_ip(ip)}


# ---------------------------------------------------------------------------
# T-BE-27: Cookie / Web Analytics Layer
# Server-side analytics pipeline: page_view, session_start, feature_usage.
# These are lightweight 1-pixel or JS-beacon style endpoints that the web
# app can fire without blocking page load.  Privacy-first: no PII stored.
# ---------------------------------------------------------------------------

class PageViewRequest:
    """Non-pydantic: parsed directly from query-params for beacon compatibility."""

class AnalyticsBeaconRequest(TelemetryEventRequest):
    """
    Extended telemetry body for web analytics events.

    Valid event_name values (web-specific):
      page_view, session_start, feature_usage, cta_click, upgrade_click
    """
    # Override to make event_name optional for beacon callers
    event_name: str = "page_view"  # type: ignore[assignment]
    page: Optional[str] = None          # route / path that was viewed
    referrer: Optional[str] = None      # HTTP referrer (scrubbed of query params)
    feature: Optional[str] = None       # which feature was used
    session_id_web: Optional[str] = None  # web session id (not the AI session)


@router.post(
    "/analytics",
    status_code=status.HTTP_202_ACCEPTED,
    summary="T-BE-27: Web analytics event (page_view, feature_usage, etc.)",
    tags=["analytics"],
)
async def ingest_analytics(
    body: AnalyticsBeaconRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """
    Server-side analytics event pipeline.

    Accepts lightweight events from the marketing site and web dashboard:
    - ``page_view``     — user viewed a page (tracks funnel)
    - ``session_start`` — authenticated user started a coding session
    - ``feature_usage`` — user clicked a specific feature (``feature=`` param)
    - ``cta_click``     — user clicked a CTA button
    - ``upgrade_click`` — user clicked "Upgrade" — high intent signal

    Privacy guarantees:
    - No cookies set or read server-side.
    - IP geolocation (country/city only, no lat/long stored).
    - Query params stripped from referrer before storage.
    - If ``privacy_mode=true`` in JWT — geo + IP suppressed.
    """
    # Strip query params from referrer for privacy
    referrer = body.referrer or ""
    if "?" in referrer:
        referrer = referrer.split("?")[0]

    # Optional anonymous user identity
    user_id: str | None = None
    privacy_mode = False
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            from app.middleware.auth import verify_pakalon_jwt  # noqa: PLC0415
            jwt_payload = verify_pakalon_jwt(auth_header[7:])
            user_id = jwt_payload.get("sub")
            privacy_mode = bool(jwt_payload.get("privacy_mode", False))
        except Exception:
            pass

    # T-BE-24: DB-authoritative privacy mode — DB value overrides JWT claim
    if user_id:
        try:
            from sqlalchemy import select  # noqa: PLC0415
            from app.models.user import User  # noqa: PLC0415
            pm_result = await session.execute(
                select(User.privacy_mode).where(User.id == user_id)
            )
            db_pm = pm_result.scalar_one_or_none()
            if db_pm is not None:
                privacy_mode = bool(db_pm)
        except Exception:
            pass  # fall back to JWT claim on DB error

    client_ip = _get_client_ip(request)
    geo_data: dict = {}
    if not privacy_mode and client_ip:
        geo_data = _geolocate_ip(client_ip)

    # Build sanitized properties
    props: dict = {
        "page": body.page or "",
        "referrer": referrer,
        "feature": body.feature or "",
        "web_session_id": body.session_id_web or "",
    }
    # Merge caller-supplied properties (strip PII keys)
    caller_props = body.sanitized_properties()
    props.update({k: v for k, v in caller_props.items() if k not in ("page", "referrer", "feature")})
    if geo_data:
        props["geo"] = {
            "country_code": geo_data.get("country_code", ""),
            "country_name": geo_data.get("country_name", ""),
            "city": geo_data.get("city", ""),
        }

    event = TelemetryEvent(
        id=str(uuid.uuid4()),
        user_id=user_id,
        event_name=body.event_name or "page_view",
        properties=props,
        cli_version=body.cli_version or "",
        os_name=body.os_name or "web",
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(event)
    await session.commit()
    return {"id": event.id, "recorded": True}


@router.get(
    "/analytics/pixel",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="T-BE-27: 1×1 tracking pixel (no-JS fallback)",
    include_in_schema=False,
)
async def tracking_pixel(
    event: str = "page_view",
    page: str = "",
    sid: str = "",
    request: Request = None,  # type: ignore[assignment]
    session: AsyncSession = Depends(get_session),
):
    """
    Minimalist 1-pixel tracking beacon for environments where JS is disabled.
    Fired via <img src="/telemetry/analytics/pixel?event=page_view&page=/pricing">
    Returns 204 No Content (no response body).
    """
    if not page and request:
        page = request.headers.get("Referer", "").split("?")[0]

    client_ip = _get_client_ip(request) if request else ""
    geo_data = _geolocate_ip(client_ip) if client_ip else {}

    ev = TelemetryEvent(
        id=str(uuid.uuid4()),
        user_id=None,
        event_name=event,
        properties={
            "page": page,
            "web_session_id": sid,
            "geo": {
                "country_code": geo_data.get("country_code", ""),
                "country_name": geo_data.get("country_name", ""),
            },
        },
        cli_version="",
        os_name="web-pixel",
        created_at=datetime.now(tz=timezone.utc),
    )
    session.add(ev)
    await session.commit()
    # 204 — no body returned
