"""Model refresh job — fetch latest models from OpenRouter (T041)."""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import make_async_engine
from app.services.model_registry import cache_models, ensure_model_cache_schema_compat, fetch_models_from_openrouter
from app.models.model_cache import ModelCache
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def run_model_refresh() -> None:
    """
    Nightly scheduled job (cron 3:00 AM).

    Fetches the current model list from OpenRouter API and refreshes the
    local model_cache table.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: PLC0415

    engine = make_async_engine(echo=False)
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        models = await fetch_models_from_openrouter()
        logger.info("Fetched %d models from OpenRouter", len(models))
        async with async_session() as session:
            await ensure_model_cache_schema_compat(session)
            # Mark all existing cache entries as potentially stale before refresh
            await session.execute(
                ModelCache.__table__.update().values(cache_valid=False)
            )
            # Cache new models and mark them as valid
            await cache_models(models, session)
            await session.commit()
        logger.info("Model cache refreshed successfully")
    except Exception as exc:
        logger.exception("Model refresh job failed: %s", exc)
        # Keep stale cache - don't update cache_valid on failure
        # Existing cache entries remain valid (but may be stale)
    finally:
        await engine.dispose()
