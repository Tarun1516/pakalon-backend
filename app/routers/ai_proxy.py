"""
ai_proxy.py — Server-side AI inference proxy using the master OpenRouter key.

Every authenticated CLI request is routed through here so users never need
their own OpenRouter API key.  The backend owns the single master key.

Endpoints:
  POST /ai/chat          — non-streaming completion (for bridge/batch)
  POST /ai/chat/stream   — SSE streaming completion (for CLI real-time)

Rate-limiting, plan-gating, and per-model context-window enforcement all
happen at this layer before the upstream call is made.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.dependencies import get_current_user
from app.models.user import User
from app.services.rate_limit import check_rate_limit, rate_limit_headers, FREE_LIMIT, PRO_LIMIT
from app.services.usage_analytics import (
    is_context_exhausted,
    record_model_usage,
    get_remaining_pct,
)
from app.services.model_registry import get_model_context_window

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["ai-proxy"])

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _coerce_openrouter_content(content: object) -> str:
    """Normalize OpenRouter content payloads into a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_part = item.get("text")
                if isinstance(text_part, str):
                    parts.append(text_part)
        return "".join(parts)
    return ""

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class AIMessage(BaseModel):
    role: str          # "system" | "user" | "assistant"
    content: str


class AIChatRequest(BaseModel):
    model: str
    messages: list[AIMessage]
    system: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    thinking_enabled: bool = False
    privacy_mode: bool = False
    session_id: Optional[str] = None
    lines_delta: Optional[int] = None   # lines of code written (for usage tracking)


class AIChatResponse(BaseModel):
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    remaining_pct: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_headers(settings, privacy_mode: bool) -> dict[str, str]:
    """Build OpenRouter request headers, respecting privacy mode."""
    headers: dict[str, str] = {
        "Authorization": f"Bearer {settings.openrouter_master_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://pakalon.com",
        "X-Title": "Pakalon CLI",
    }
    if privacy_mode:
        # T-BE-PRIVACY: Prevent prompt storage / model training on provider side.
        # X-OpenRouter-No-Prompt-Training is the canonical OpenRouter opt-out header.
        # X-No-Store + X-Training-Opt-Out are included for broader provider coverage.
        headers["X-OpenRouter-No-Prompt-Training"] = "true"
        headers["X-Training-Opt-Out"] = "true"
        headers["X-No-Store"] = "true"
    return headers


def _gated_model(model: str, user_plan: str) -> str:
    """
    Enforce plan gating at the proxy layer.
    Free-plan users are restricted to ':free' tier models only.
    Returns the model string unchanged for pro users.
    Raises HTTP 403 for plan violations.
    """
    if user_plan == "pro":
        return model
    # Free plan: only :free tier allowed
    if not model.endswith(":free"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Model '{model}' requires a Pro plan. "
                "Upgrade at https://pakalon.com/pricing or switch to a free model."
            ),
        )
    return model


async def _check_context_window(
    user_id: str,
    model: str,
    session: AsyncSession,
    model_display: str,
) -> None:
    """Raise 429 with the exact required error format if context is exhausted."""
    exhausted = await is_context_exhausted(user_id, model, session)
    if exhausted:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"{model_display} Models context windows is used completely, "
                "switch to another model to use the application"
            ),
            headers={"X-Pakalon-Context-Exhausted": "true"},
        )


def _build_openrouter_payload(
    model: str,
    messages: list[AIMessage],
    system: str | None,
    max_tokens: int | None,
    temperature: float | None,
    thinking_enabled: bool,
    stream: bool,
) -> dict:
    """Construct the OpenRouter chat completion payload."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend({"role": m.role, "content": m.content} for m in messages)

    payload: dict = {
        "model": model,
        "messages": msgs,
        "stream": stream,
    }

    if thinking_enabled:
        payload["max_tokens"] = max_tokens or 16000
        payload["temperature"] = 1.0
        # Enable extended reasoning on supporting models (Claude, DeepSeek-R1…)
        payload["reasoning"] = {"effort": "high"}
    else:
        payload["max_tokens"] = max_tokens or 4096
        payload["temperature"] = temperature if temperature is not None else 0.7

    return payload


# ---------------------------------------------------------------------------
# Non-streaming endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/chat",
    response_model=AIChatResponse,
    summary="AI inference via master key (non-streaming)",
)
async def ai_chat(
    body: AIChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    # T-CLI-P10: X-Privacy-Mode header — CLI passes this header to signal
    # that OpenRouter must not log/train on this request.
    x_privacy_mode: str | None = Header(default=None, alias="X-Privacy-Mode"),
):
    """
    Route an AI completion request through the backend master OpenRouter key.

    - Enforces plan gating (free → only :free models)
    - Enforces per-model context window limits
    - Records token usage and line delta to model_usage table
    """
    settings = get_settings()
    if not settings.openrouter_master_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI proxy not configured (OPENROUTER_MASTER_KEY is unset)",
        )

    model = _gated_model(body.model, current_user.plan)
    model_display = model.split("/")[-1].split(":")[0]
    await _check_context_window(current_user.id, model, db, model_display)

    # T-BE-22: Redis sliding-window rate limit (non-streaming)
    try:
        from app.main import redis_client  # noqa: PLC0415
        if redis_client is not None:
            _allowed, _remaining, _retry_after = await check_rate_limit(
                redis_client, current_user.id, current_user.plan
            )
            if not _allowed:
                _plan_limit = PRO_LIMIT if current_user.plan in ("pro", "enterprise") else FREE_LIMIT
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded: {_plan_limit} requests/minute. Retry in {_retry_after}s.",
                    headers=rate_limit_headers(0, _plan_limit, _retry_after),
                )
    except HTTPException:
        raise
    except Exception as _rl_err:
        logger.debug("Rate-limit check skipped (Redis unavailable): %s", _rl_err)

    # T-CLI-P10: merge header-based privacy mode with body-based flag (also honour user-level privacy_mode)
    effective_privacy = body.privacy_mode or current_user.privacy_mode or (x_privacy_mode is not None and x_privacy_mode not in ("0", "false", ""))

    payload = _build_openrouter_payload(
        model=model,
        messages=body.messages,
        system=body.system,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        thinking_enabled=body.thinking_enabled,
        stream=False,
    )
    headers = _build_headers(settings, effective_privacy)

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{OPENROUTER_BASE}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError as exc:
                logger.error("OpenRouter returned invalid JSON payload: %s", resp.text[:1000])
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Upstream AI provider returned an invalid response",
                ) from exc
    except httpx.HTTPStatusError as exc:
        logger.error("OpenRouter error %s: %s", exc.response.status_code, exc.response.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Upstream AI provider error: {exc.response.status_code}",
        ) from exc
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI provider timed out",
        )

    choices = data.get("choices") or []
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
    content = _coerce_openrouter_content(message.get("content"))
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # Look up model context window size from cache
    ctx_window = await get_model_context_window(model, db)
    total_tokens = prompt_tokens + completion_tokens

    # Record usage (best-effort; completion should still return even if usage writes fail)
    try:
        await record_model_usage(
            user_id=current_user.id,
            session_id=body.session_id,
            model_id=model,
            tokens_used=total_tokens,
            context_window_size=ctx_window,
            context_window_used=prompt_tokens,
            lines_written=body.lines_delta or 0,
            db=db,
        )
        await db.commit()
    except Exception as exc:
        logger.warning("Usage record failed after non-stream response: %s", exc)

    try:
        remaining = await get_remaining_pct(current_user.id, model, db)
    except Exception as exc:
        logger.warning("Remaining context fetch failed: %s", exc)
        remaining = None

    return AIChatResponse(
        content=content,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        remaining_pct=float(remaining) if remaining is not None else 100.0,
    )


# ---------------------------------------------------------------------------
# Streaming endpoint (SSE)
# ---------------------------------------------------------------------------

@router.post(
    "/chat/stream",
    summary="AI inference via master key (SSE streaming)",
)
async def ai_chat_stream(
    body: AIChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    # T-CLI-P10: X-Privacy-Mode header support
    x_privacy_mode: str | None = Header(default=None, alias="X-Privacy-Mode"),
):
    """
    Stream AI completions as Server-Sent Events.

    Emits:
      data: {"type": "chunk", "content": "..."}
      data: {"type": "done", "prompt_tokens": N, "completion_tokens": M, "remaining_pct": P}
      data: {"type": "error", "detail": "..."}
    """
    settings = get_settings()
    if not settings.openrouter_master_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI proxy not configured (OPENROUTER_MASTER_KEY is unset)",
        )

    model = _gated_model(body.model, current_user.plan)
    model_display = model.split("/")[-1].split(":")[0]
    await _check_context_window(current_user.id, model, db, model_display)

    # T-BE-22: Redis sliding-window rate limit (streaming)
    try:
        from app.main import redis_client  # noqa: PLC0415
        if redis_client is not None:
            _allowed, _remaining, _retry_after = await check_rate_limit(
                redis_client, current_user.id, current_user.plan
            )
            if not _allowed:
                _plan_limit = PRO_LIMIT if current_user.plan in ("pro", "enterprise") else FREE_LIMIT
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded: {_plan_limit} requests/minute. Retry in {_retry_after}s.",
                    headers=rate_limit_headers(0, _plan_limit, _retry_after),
                )
    except HTTPException:
        raise
    except Exception as _rl_err:
        logger.debug("Rate-limit check skipped (Redis unavailable): %s", _rl_err)

    # T-CLI-P10: merge header-based privacy mode with body-based flag (also honour user-level privacy_mode)
    effective_privacy = body.privacy_mode or current_user.privacy_mode or (x_privacy_mode is not None and x_privacy_mode not in ("0", "false", ""))

    payload = _build_openrouter_payload(
        model=model,
        messages=body.messages,
        system=body.system,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        thinking_enabled=body.thinking_enabled,
        stream=True,
    )
    headers = _build_headers(settings, effective_privacy)

    async def _sse_generator() -> AsyncIterator[str]:
        prompt_tokens = 0
        completion_tokens = 0
        full_content = ""

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST",
                    f"{OPENROUTER_BASE}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        body_text = await resp.aread()
                        yield f"data: {json.dumps({'type': 'error', 'detail': f'Upstream {resp.status_code}: {body_text.decode()}'})}\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:]
                        if raw.strip() == "[DONE]":
                            break
                        try:
                            chunk_data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if chunk_data.get("error"):
                            detail = chunk_data["error"].get("message") if isinstance(chunk_data["error"], dict) else str(chunk_data["error"])
                            yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                            return

                        # Extract text delta
                        choices = chunk_data.get("choices") or []
                        first_choice = choices[0] if choices else {}
                        delta_obj = first_choice.get("delta", {}) if isinstance(first_choice, dict) else {}
                        delta = _coerce_openrouter_content(delta_obj.get("content"))
                        if delta:
                            full_content += delta
                            yield f"data: {json.dumps({'type': 'chunk', 'content': delta})}\n\n"

                        # Capture usage from final chunk
                        usage_obj = chunk_data.get("usage", {})
                        if usage_obj:
                            prompt_tokens = usage_obj.get("prompt_tokens", 0)
                            completion_tokens = usage_obj.get("completion_tokens", 0)

        except httpx.TimeoutException:
            yield f"data: {json.dumps({'type': 'error', 'detail': 'AI provider timed out'})}\n\n"
            return
        except Exception as exc:
            logger.exception("AI proxy stream error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
            return

        # Record usage after stream finishes
        try:
            ctx_window = await get_model_context_window(model, db)
            async with db.begin_nested():
                await record_model_usage(
                    user_id=current_user.id,
                    session_id=body.session_id,
                    model_id=model,
                    tokens_used=prompt_tokens + completion_tokens,
                    context_window_size=ctx_window,
                    context_window_used=prompt_tokens,
                    lines_written=body.lines_delta or 0,
                    db=db,
                )
            await db.commit()
        except Exception as exc:
            logger.warning("Usage record failed after stream: %s", exc)

        remaining = await get_remaining_pct(current_user.id, model, db)
        remaining_val = float(remaining) if remaining is not None else 100.0
        yield f"data: {json.dumps({'type': 'done', 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'remaining_pct': remaining_val})}\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
