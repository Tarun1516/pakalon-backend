"""APScheduler setup — background job scheduler."""
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.executors.asyncio import AsyncIOExecutor

from app.config import get_settings

settings = get_settings()

scheduler = AsyncIOScheduler(
    jobstores={"default": MemoryJobStore()},
    executors={"default": AsyncIOExecutor()},
    job_defaults={
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    },
    timezone=settings.scheduler_timezone,
)


def register_jobs() -> None:
    """Register all background jobs with the scheduler."""
    from app.jobs.model_refresh import run_model_refresh
    from app.jobs.expiry_checker import run_expiry_checker
    from app.jobs.email_queue import run_email_queue
    from app.jobs.geoip_update import run_geoip_update

    # Every 6 hours — refresh OpenRouter model cache (T-BE-13)
    scheduler.add_job(
        run_model_refresh,
        trigger="interval",
        hours=6,
        id="model_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        run_model_refresh,
        trigger="cron",
        hour=3,
        minute=0,
        id="model_refresh",
        replace_existing=True,
    )

    # Daily at 0:30 AM UTC — check for expiring trials/subscriptions
    scheduler.add_job(
        run_expiry_checker,
        trigger="cron",
        hour=0,
        minute=30,
        id="expiry_checker",
        replace_existing=True,
    )

    # Every 5 minutes — send pending emails from queue
    scheduler.add_job(
        run_email_queue,
        trigger="interval",
        minutes=5,
        id="email_queue",
        replace_existing=True,
    )

    # Weekly (Sunday 04:00 UTC) — refresh MaxMind GeoLite2-City DB (T-BE-21)
    scheduler.add_job(
        run_geoip_update,
        trigger="cron",
        day_of_week="sun",
        hour=4,
        minute=0,
        id="geoip_update",
        replace_existing=True,
    )


# Register jobs when module is first imported
register_jobs()
