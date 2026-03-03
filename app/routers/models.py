"""Models router — list available AI models (T040, T-BACK-01, T-BACK-07)."""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.user import User
from app.services.model_registry import get_models_for_plan, pick_auto_model
from app.services.usage_analytics import get_remaining_pct, get_context_status
from app.jobs.model_refresh import run_model_refresh

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/models", tags=["models"])


@router.get(
    "",
    summary="List available models for authenticated user's plan",
)
async def list_models(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Return all models available for the user's current plan.

    - Free users: only :free tier models  (T-BACK-07)
    - Pro users: all models
    Each model includes remaining_pct (context window % remaining, T-BACK-01).
    """
    models = await get_models_for_plan(current_user.plan, session)

    # Enrich with context window remaining_pct
    enriched = []
    for m in models:
        model_id = m.get("model_id") or m.get("id", "")
        pct = await get_remaining_pct(current_user.id, model_id, session)
        enriched.append({**m, "remaining_pct": pct})

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
