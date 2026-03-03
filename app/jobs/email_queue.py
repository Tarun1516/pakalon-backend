"""Email queue processor job (T150)."""
import logging

from app.services.email import process_email_queue

logger = logging.getLogger(__name__)


async def run_email_queue() -> None:
    """
    Scheduled job (interval 5 minutes).

    Drains the pending email queue by sending emails via Resend.
    """
    from app.config import get_settings  # noqa: PLC0415
    from app.database import create_async_engine  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with async_session() as session:
            sent_count = await process_email_queue(session)
        if sent_count:
            logger.info("Email queue: sent %d emails", sent_count)
    except Exception as exc:
        logger.exception("Email queue job failed: %s", exc)
    finally:
        await engine.dispose()
