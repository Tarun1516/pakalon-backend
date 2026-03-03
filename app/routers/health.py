"""Health check router (T161) — DB + Redis probe."""
import logging
from importlib.metadata import version, PackageNotFoundError

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.database import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_redis():
    try:
        from app.main import redis_client  # noqa: PLC0415
        return redis_client
    except Exception:
        return None


def _app_version() -> str:
    try:
        return version("pakalon-backend")
    except PackageNotFoundError:
        return "0.1.0"


@router.get("/health", tags=["health"])
async def health(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """
    Deep health check — verifies app, DB, and Redis are all reachable.

    Returns HTTP 200 with all statuses on success, or HTTP 503 if any
    dependency is unavailable.
    """
    db_status = "ok"
    redis_status = "ok"

    # DB probe
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Health check DB probe failed: %s", exc)
        db_status = "error"

    # Redis probe
    redis = _get_redis()
    if redis is not None:
        try:
            await redis.ping()
        except Exception as exc:
            logger.error("Health check Redis probe failed: %s", exc)
            redis_status = "error"
    else:
        redis_status = "unavailable"

    overall = "ok" if db_status == "ok" and redis_status == "ok" else "degraded"
    http_code = 200 if overall == "ok" else 503

    return JSONResponse(
        status_code=http_code,
        content={
            "status": overall,
            "service": "pakalon-backend",
            "version": _app_version(),
            "db": db_status,
            "redis": redis_status,
        },
    )
