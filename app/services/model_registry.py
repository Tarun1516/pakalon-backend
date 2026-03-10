"""Model registry service — fetch and cache OpenRouter models (T039)."""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.model_cache import ModelCache


def _parse_openrouter_created(model_data: dict[str, Any]) -> datetime | None:
    """
    Parse OpenRouter's `created` field (Unix epoch int) into a timezone-aware datetime.
    Returns None when the field is absent or unparseable.
    """
    raw = model_data.get("created")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
CACHE_TTL_HOURS = 24

# Plan → model tier mapping
PLAN_MODEL_TIERS = {
    "free": ["free"],        # Only :free suffix models
    "pro": ["free", "paid"], # All models
    "enterprise": ["free", "paid"],
}


async def ensure_model_cache_schema_compat(session: AsyncSession) -> set[str]:
    """Upgrade older model_cache table layouts in-place before ORM queries run."""

    def _load_columns(sync_session) -> set[str]:
        inspector = inspect(sync_session.connection())
        if "model_cache" not in inspector.get_table_names():
            return set()
        return {column["name"] for column in inspector.get_columns("model_cache")}

    columns = await session.run_sync(_load_columns)
    if not columns:
        return columns

    dialect = session.get_bind().dialect.name
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "TIMESTAMP"
    true_literal = "TRUE" if dialect == "postgresql" else "1"
    statements: list[str] = []

    if "context_length" not in columns:
        statements.append("ALTER TABLE model_cache ADD COLUMN context_length INTEGER NOT NULL DEFAULT 0")
        columns.add("context_length")
    if "tier" not in columns:
        statements.append("ALTER TABLE model_cache ADD COLUMN tier VARCHAR(20) NOT NULL DEFAULT 'paid'")
        columns.add("tier")
    if "fetched_at" not in columns:
        statements.append(
            f"ALTER TABLE model_cache ADD COLUMN fetched_at {timestamp_type} NULL"
        )
        columns.add("fetched_at")
    if "model_created_at" not in columns:
        statements.append(
            f"ALTER TABLE model_cache ADD COLUMN model_created_at {timestamp_type} NULL"
        )
        columns.add("model_created_at")
    if "cache_valid" not in columns:
        statements.append(
            f"ALTER TABLE model_cache ADD COLUMN cache_valid BOOLEAN NOT NULL DEFAULT {true_literal}"
        )
        columns.add("cache_valid")

    changed = False
    for statement in statements:
        await session.execute(text(statement))
        changed = True

    assignments: list[str] = []
    if "context_length" in columns and "context_window" in columns:
        assignments.append(
            "context_length = CASE "
            "WHEN context_length IS NULL OR context_length = 0 THEN COALESCE(context_window, 0) "
            "ELSE context_length END"
        )
    if "tier" in columns:
        if "pricing_tier" in columns:
            assignments.append(
                "tier = CASE "
                "WHEN (tier IS NULL OR tier = '') AND pricing_tier IS NOT NULL AND LOWER(pricing_tier) = 'free' THEN 'free' "
                "WHEN (tier IS NULL OR tier = '') AND pricing_tier IS NOT NULL THEN 'paid' "
                "WHEN tier = 'paid' AND pricing_tier IS NOT NULL AND LOWER(pricing_tier) = 'free' THEN 'free' "
                "WHEN tier IS NULL OR tier = '' THEN 'paid' "
                "ELSE tier END"
            )
        else:
            assignments.append(
                "tier = CASE WHEN tier IS NULL OR tier = '' THEN 'paid' ELSE tier END"
            )
    if "fetched_at" in columns and "updated_at" in columns:
        assignments.append("fetched_at = COALESCE(fetched_at, updated_at, CURRENT_TIMESTAMP)")
    elif "fetched_at" in columns:
        assignments.append("fetched_at = COALESCE(fetched_at, CURRENT_TIMESTAMP)")
    if "cache_valid" in columns:
        assignments.append(f"cache_valid = COALESCE(cache_valid, {true_literal})")

    if assignments:
        await session.execute(text(f"UPDATE model_cache SET {', '.join(assignments)}"))
        changed = True

    if changed:
        await session.commit()

    return columns


async def fetch_models_from_openrouter() -> list[dict[str, Any]]:
    """Fetch the current model list from OpenRouter API."""
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.openrouter_master_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(OPENROUTER_MODELS_URL, headers=headers)
        response.raise_for_status()
        data = response.json()
    return data.get("data", [])


def _classify_model(model: dict[str, Any]) -> str:
    """Classify a model as 'free' or 'paid' based on pricing."""
    pricing = model.get("pricing", {})
    prompt_cost = float(pricing.get("prompt", "0") or 0)
    completion_cost = float(pricing.get("completion", "0") or 0)
    model_id: str = model.get("id", "")
    if model_id.endswith(":free") or (prompt_cost == 0 and completion_cost == 0):
        return "free"
    return "paid"


async def cache_models(models: list[dict[str, Any]], session: AsyncSession) -> None:
    """Upsert model records in the database."""
    await ensure_model_cache_schema_compat(session)
    now = datetime.now(tz=timezone.utc)
    for model_data in models:
        model_id = model_data.get("id", "")
        if not model_id:
            continue
        tier = _classify_model(model_data)

        result = await session.execute(
            select(ModelCache).where(ModelCache.model_id == model_id)
        )
        cached = result.scalar_one_or_none()

        model_created_at = _parse_openrouter_created(model_data)

        if cached is None:
            cached = ModelCache(
                model_id=model_id,
                name=model_data.get("name", model_id),
                context_length=model_data.get("context_length", 0),
                tier=tier,
                raw_json=json.dumps(model_data),
                fetched_at=now,
                model_created_at=model_created_at,
                cache_valid=True,  # Newly fetched models are valid
            )
            session.add(cached)
        else:
            cached.name = model_data.get("name", model_id)
            cached.context_length = model_data.get("context_length", 0)
            cached.tier = tier
            cached.raw_json = json.dumps(model_data)
            cached.fetched_at = now
            cached.cache_valid = True  # Mark as valid after successful refresh
            # Always refresh model_created_at in case OpenRouter back-fills it
            if model_created_at is not None:
                cached.model_created_at = model_created_at

    await session.flush()


async def get_models_for_plan(
    plan: str,
    session: AsyncSession,
    *,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """Return cached models appropriate for the given plan."""
    await ensure_model_cache_schema_compat(session)
    tiers = ["free", "paid"] if include_all else PLAN_MODEL_TIERS.get(plan, ["free"])

    # T041: Sort newest models first.
    # Primary key: model_created_at (OpenRouter release date) DESC — newest model first.
    # Fallback for rows where model_created_at is NULL (old cache): fetched_at DESC.
    # Secondary key: context_length DESC (largest context window first within same release).
    from sqlalchemy import case, nullslast  # noqa: PLC0415
    result = await session.execute(
        select(ModelCache)
        .where(ModelCache.tier.in_(tiers))
        .order_by(
            nullslast(ModelCache.model_created_at.desc()),
            ModelCache.context_length.desc(),
        )
    )
    cached_models = result.scalars().all()

    if not cached_models:
        return []

    models_list = []
    for m in cached_models:
        try:
            raw = json.loads(m.raw_json) if m.raw_json else {}
        except json.JSONDecodeError:
            raw = {}
        models_list.append(
            {
                "id": m.model_id,
                "name": m.name,
                "context_length": m.context_length,
                "tier": m.tier,
                **raw,
            }
        )
    return models_list


async def is_cache_stale(session: AsyncSession) -> bool:
    """Return True if the model cache is older than CACHE_TTL_HOURS."""
    await ensure_model_cache_schema_compat(session)
    result = await session.execute(
        select(ModelCache).order_by(ModelCache.fetched_at.desc()).limit(1)
    )
    latest = result.scalar_one_or_none()
    if latest is None:
        return True
    fetched_at = latest.fetched_at
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    threshold = datetime.now(tz=timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)
    return fetched_at < threshold


async def get_model_context_window(model_id: str, session: AsyncSession) -> int:
    """
    Return the context window size for a model_id.
    Falls back to 4096 if the model is not in cache.
    """
    await ensure_model_cache_schema_compat(session)
    result = await session.execute(
        select(ModelCache).where(ModelCache.model_id == model_id)
    )
    cached = result.scalar_one_or_none()
    if cached and cached.context_length:
        return cached.context_length
    return 4096  # Safe default



def pick_auto_model(plan: str, models: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Select the recommended 'auto' model for a plan.

    Strategy:
    - Free: choose cheapest free model, tie-break by largest context window.
    - Pro/Enterprise: choose the lowest effective token-cost model that still has
      practical context (>= 64k where possible), tie-break by larger context.
    """
    if not models:
        return None

    def _cost_score(m: dict[str, Any]) -> float:
        pricing = m.get("pricing") or {}
        try:
            prompt_cost = float(pricing.get("prompt", 0) or 0)
        except (TypeError, ValueError):
            prompt_cost = 0.0
        try:
            completion_cost = float(pricing.get("completion", 0) or 0)
        except (TypeError, ValueError):
            completion_cost = 0.0
        # Slight completion bias to match common chat workloads.
        return prompt_cost + (completion_cost * 1.5)

    def _ctx(m: dict[str, Any]) -> int:
        try:
            return int(m.get("context_length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    if plan == "free":
        free_models = [m for m in models if m.get("tier") == "free"]
        if not free_models:
            return None
        return sorted(free_models, key=lambda m: (_cost_score(m), -_ctx(m)))[0]

    # Pro/Enterprise: prefer paid models with at least 64k context where possible,
    # then optimize for cost.
    paid_models = [m for m in models if m.get("tier") == "paid"]
    candidate_pool = paid_models or models
    wide_context = [m for m in candidate_pool if _ctx(m) >= 64000]
    ranked_pool = wide_context or candidate_pool
    if not ranked_pool:
        return None
    return sorted(ranked_pool, key=lambda m: (_cost_score(m), -_ctx(m)))[0]
