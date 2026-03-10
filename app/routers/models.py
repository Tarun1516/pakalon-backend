"""Models router — list available AI models (T040, T-BACK-01, T-BACK-07)."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.model_usage import ModelUsage
from app.models.user import User
from app.services.model_registry import get_models_for_plan, is_cache_stale, pick_auto_model
from app.services.usage_analytics import get_context_status
from app.jobs.model_refresh import run_model_refresh

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/models", tags=["models"])


@router.get(
    "",
    summary="List available models for authenticated user's plan",
)
async def list_models(
    include_all: bool = Query(
        default=False,
        description="Return the full cached OpenRouter catalog instead of the current plan-filtered subset",
    ),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Return all models available for the user's current plan.

    - Free users: only :free tier models  (T-BACK-07)
    - Pro users: all models
    Each model includes remaining_pct (context window % remaining, T-BACK-01).
    """
    models = await get_models_for_plan(current_user.plan, session, include_all=include_all)
    should_refresh = not models
    if not should_refresh:
        try:
            should_refresh = await is_cache_stale(session)
        except Exception as exc:  # pragma: no cover - best-effort staleness check
            logger.warning("Models cache staleness check failed during list_models: %s", exc)

    if should_refresh:
        try:
            await run_model_refresh()
            models = await get_models_for_plan(current_user.plan, session, include_all=include_all)
        except Exception as exc:  # pragma: no cover - best-effort recovery
            logger.warning("Models cache refresh failed during list_models: %s", exc)

    latest_usage_subquery = (
        select(
            ModelUsage.model_id.label("model_id"),
            func.max(ModelUsage.created_at).label("latest_created_at"),
        )
        .where(
            ModelUsage.user_id == current_user.id,
            ModelUsage.context_window_size > 0,
        )
        .group_by(ModelUsage.model_id)
        .subquery()
    )

    remaining_pct_by_model: dict[str, int] = {}
    try:
        usage_rows = await session.execute(
            select(
                ModelUsage.model_id,
                ModelUsage.context_window_used,
                ModelUsage.context_window_size,
            )
            .join(
                latest_usage_subquery,
                and_(
                    ModelUsage.model_id == latest_usage_subquery.c.model_id,
                    ModelUsage.created_at == latest_usage_subquery.c.latest_created_at,
                ),
            )
            .where(
                ModelUsage.user_id == current_user.id,
                ModelUsage.context_window_size > 0,
            )
        )
        for row in usage_rows:
            total = int(row.context_window_size or 0)
            if total <= 0:
                continue
            used = int(row.context_window_used or 0)
            remaining_pct_by_model[row.model_id] = max(0, 100 - round(used / total * 100))
    except (OperationalError, ProgrammingError) as exc:
        error_text = str(getattr(exc, "orig", exc)).lower()
        missing_table_markers = (
            "no such table: model_usage",
            'relation "model_usage" does not exist',
            "undefinedtable",
        )
        if any(marker in error_text for marker in missing_table_markers):
            logger.warning("model_usage table is unavailable; returning models without remaining_pct data")
        else:
            raise

    # Enrich with context window remaining_pct
    enriched = []
    for m in models:
        model_id = m.get("model_id") or m.get("id", "")
        enriched.append({
            **m,
            "remaining_pct": remaining_pct_by_model.get(model_id),
        })

    return {"models": enriched, "plan": current_user.plan, "count": len(enriched)}


@router.get(
    "/auto",
    summary="Get recommended auto-select model for current plan",
)
async def get_auto_model(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Return the single best-fit model for the user's plan.

    The CLI uses this for the default model selection.
    """
    models = await get_models_for_plan(current_user.plan, session)
    auto = pick_auto_model(current_user.plan, models)
    if auto is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Model cache is empty — please wait for the next refresh",
        )
    return auto


@router.get(
    "/{model_id}/context",
    summary="Check context window status for a specific model",
)
async def get_model_context_status(
    model_id: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Return context window status for the authenticated user + given model.

    T-BACK-06 / T-BACK-09: Clients (bridge, CLI) call this before starting AI
    inference to check whether the context window is exhausted.

    Returns:
        { model_id, remaining_pct, exhausted, message }

    When exhausted=True the caller should:
      - Display the exhaustion message to the user
      - Block new AI generation with this model
      - Suggest starting a new session or switching models
    """
    ctx = await get_context_status(current_user.id, model_id, session)
    if ctx["exhausted"]:
        # Return 429 so that bridge / CLI can catch it without an explicit
        # exhausted=True check — both work.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ctx["message"],
            headers={"X-Pakalon-Context-Exhausted": "true"},
        )
    return ctx


@router.post(
    "/refresh",
    summary="Manually trigger model cache refresh (admin only)",
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_models(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Manually trigger a model cache refresh from OpenRouter.

    This endpoint is restricted to admin users. The refresh runs asynchronously
    and returns immediately with a 202 status.

    On failure, the existing cache remains unchanged (stale cache is preserved).
    """
    from app.models.user import User as UserModel

    # Check admin status
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins can trigger model refresh",
        )

    # Run refresh in background (fire and forget)
    import asyncio
    asyncio.create_task(run_model_refresh())

    return {"status": "refresh_started", "message": "Model refresh triggered"}
