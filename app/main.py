"""Pakalon Backend — FastAPI application factory."""
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings

logger = logging.getLogger(__name__)

# Global Redis client — initialized in lifespan
redis_client = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup + shutdown."""
    global redis_client
    settings = get_settings()
    logger.info("Starting Pakalon Backend (%s)", settings.environment)

    # Initialize Redis
    import redis.asyncio as aioredis  # noqa: PLC0415
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    # Start APScheduler background jobs
    from app.scheduler import scheduler  # noqa: PLC0415
    scheduler.start()
    logger.info("APScheduler started")

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
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Geo-blocking (T-A37) ─────────────────────────────────────
    from app.middleware.geo_block import GeoBlockMiddleware
    app.add_middleware(GeoBlockMiddleware)

    # ── Routers ───────────────────────────────────────────────
    from app.routers import auth, users, models, sessions, usage, telemetry, billing, webhooks, health, support, tools, admin, ai_proxy, media, figma, audit, notifications, credits, dashboard  # noqa: E501

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

    # ── Global exception handler ──────────────────────────────
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception on %s %s", request.method, request.url)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )

    return app


app = create_app()
