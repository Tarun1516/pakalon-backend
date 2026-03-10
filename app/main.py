"""Pakalon Backend — FastAPI application factory."""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import DATABASE_UNAVAILABLE_DETAIL, initialize_database_if_needed, is_database_unavailable_error

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Global Redis client — initialized in lifespan
redis_client = None


async def _create_redis_client(settings):
    import redis.asyncio as aioredis  # noqa: PLC0415

    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.ping()
        logger.info("Redis connected")
        return client
    except Exception:
        await client.aclose()
        if settings.is_development and settings.development_allow_fakeredis_fallback:
            from fakeredis.aioredis import FakeRedis  # noqa: PLC0415

            logger.warning(
                "Redis at %s is unavailable; using in-memory fakeredis fallback for development",
                settings.redis_url,
            )
            return FakeRedis(decode_responses=True)

        logger.warning("Redis at %s is unavailable; continuing without Redis", settings.redis_url)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup + shutdown."""
    global redis_client
    settings = get_settings()
    logger.info("Starting Pakalon Backend (%s)", settings.environment)

    await initialize_database_if_needed()
    redis_client = await _create_redis_client(settings)

    # Start APScheduler background jobs
    from app.scheduler import scheduler  # noqa: PLC0415
    scheduler.start()
    logger.info("APScheduler started")

    from app.services.automations import restore_automation_jobs  # noqa: PLC0415

    try:
        await restore_automation_jobs()
        logger.info("Automation jobs restored")
    except Exception:
        logger.exception(
            "Automation job restoration failed during startup; continuing without scheduled automation rehydration"
        )

    yield  # ← server is running here

    # Shutdown
    scheduler.shutdown(wait=False)
    if redis_client:
        await redis_client.aclose()
    logger.info("APScheduler stopped; backend shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Pakalon API",
        description="AI-Powered CLI Code Editor — Backend API",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_origin_regex=(r"https?://(localhost|127\.0\.0\.1)(:\d+)?$" if settings.is_development else None),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Geo-blocking (T-A37) ─────────────────────────────────────
    from app.middleware.geo_block import GeoBlockMiddleware
    app.add_middleware(GeoBlockMiddleware)

    # ── Routers ───────────────────────────────────────────────
    from app.routers import auth, users, models, sessions, usage, telemetry, billing, webhooks, health, support, tools, admin, ai_proxy, media, figma, audit, notifications, credits, dashboard, automations  # noqa: E501

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(models.router)
    app.include_router(sessions.router)
    app.include_router(usage.router)
    app.include_router(telemetry.router)
    app.include_router(billing.router)
    app.include_router(webhooks.router)
    app.include_router(support.router)
    app.include_router(tools.router)
    app.include_router(admin.router)
    app.include_router(ai_proxy.router)
    app.include_router(media.router)
    app.include_router(figma.router)
    app.include_router(audit.router)
    app.include_router(notifications.router)
    app.include_router(credits.router)
    app.include_router(dashboard.router)
    app.include_router(automations.router)

    # ── Global exception handler ──────────────────────────────
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        if is_database_unavailable_error(exc):
            logger.warning(
                "Database unavailable while handling %s %s: %s",
                request.method,
                request.url,
                exc,
            )
            return JSONResponse(
                status_code=503,
                content={"detail": DATABASE_UNAVAILABLE_DETAIL},
            )

        logger.exception("Unhandled exception on %s %s", request.method, request.url)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()
