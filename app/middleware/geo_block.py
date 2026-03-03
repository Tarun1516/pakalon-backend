"""
Geo-blocking middleware — IP-based geographic access restrictions (T-A37).

This middleware blocks or redirects requests from restricted regions based on
the user's IP address. Configuration is stored in the database or environment.

Features:
- Allow-list mode: only allow requests from specified countries
- Block-list mode: block requests from specified countries
- Configurable via environment or database
- Exempts admin API key requests
"""

import logging
from typing import Callable

import httpx
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings

logger = logging.getLogger(__name__)


# Default blocked countries (high-risk for abuse)
DEFAULT_BLOCKED_COUNTRIES = {"CN", "RU", "KP", "IR", "SY"}


def _get_client_ip(request: Request) -> str:
    """Extract real client IP from request headers (respects proxies)."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return ""


async def _geolocate_ip(ip: str) -> str | None:
    """Get country code for an IP address."""
    if not ip or ip.startswith(("127.", "10.", "192.168.", "172.")):
        return None  # Local IPs

    try:
        # Use ip-api.com (free tier)
        resp = httpx.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "countryCode"},
            timeout=3,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return data.get("countryCode")
    except Exception:
        pass

    return None


class GeoBlockMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce IP-based geographic access restrictions.

    Configuration (via environment):
    - GEO_BLOCK_MODE: "allow" | "block" (default: "block")
    - GEO_ALLOWED_COUNTRIES: comma-separated country codes (for allow mode)
    - GEO_BLOCKED_COUNTRIES: comma-separated country codes (for block mode)
    - GEO_BLOCK_ENABLED: "true" | "false" (default: false, disabled)
    """

    def __init__(self, app, enabled: bool = False) -> None:
        super().__init__(app)
        settings = get_settings()

        self._enabled = enabled or getattr(settings, "geo_block_enabled", False)
        self._mode = getattr(settings, "geo_block_mode", "block")
        self._allowed = (
            set(getattr(settings, "geo_allowed_countries", "").split(","))
            if getattr(settings, "geo_allowed_countries", "")
            else set()
        )
        self._blocked = (
            set(getattr(settings, "geo_blocked_countries", "").split(","))
            if getattr(settings, "geo_blocked_countries", "")
            else DEFAULT_BLOCKED_COUNTRIES
        )

        logger.info(
            "[GeoBlock] Initialized: enabled=%s, mode=%s, allowed=%s, blocked=%s",
            self._enabled,
            self._mode,
            self._allowed,
            self._blocked,
        )

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Skip if disabled
        if not self._enabled:
            return await call_next(request)

        # Skip admin endpoints
        if request.url.path.startswith("/admin"):
            return await call_next(request)

        # Skip health checks
        if request.url.path in ("/health", "/healthz"):
            return await call_next(request)

        # Skip if admin API key present (admin requests always allowed)
        admin_key = request.headers.get("x-admin-key")
        if admin_key:
            return await call_next(request)

        # Get client IP
        client_ip = _get_client_ip(request)
        if not client_ip:
            # No IP found - allow but log
            logger.debug("[GeoBlock] No client IP found, allowing request")
            return await call_next(request)

        # Geolocate the IP
        country_code = await _geolocate_ip(client_ip)

        if not country_code:
            # Couldn't geolocate - allow but log warning
            logger.warning("[GeoBlock] Could not geolocate IP %s, allowing", client_ip)
            return await call_next(request)

        # Check against allow/block list
        should_block = False

        if self._mode == "allow":
            # Allow-list mode: block if NOT in allowed list
            if country_code not in self._allowed:
                should_block = True
                logger.info(
                    "[GeoBlock] Blocking request from %s (country %s not in allowlist)",
                    client_ip,
                    country_code,
                )
        else:
            # Block-list mode: block if IN blocked list
            if country_code in self._blocked:
                should_block = True
                logger.info(
                    "[GeoBlock] Blocking request from %s (country %s in blocklist)",
                    client_ip,
                    country_code,
                )

        if should_block:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Access denied from your region",
                    "code": "GEO_BLOCKED",
                    "country": country_code,
                },
            )

        return await call_next(request)
