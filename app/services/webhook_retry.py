"""
Webhook retry helper — exponential-backoff wrapper + dead-letter persistence.

Usage
-----
    from app.services.webhook_retry import with_retry, record_dead_letter

    # Wrap any coroutine factory with retry:
    result = await with_retry(
        lambda: polar.checkouts.create(request={...}),
        service="polar",
        operation="checkouts.create",
        payload={"product_price_id": ...},
        session=db_session,
    )

    # Or record a dead-letter manually:
    await record_dead_letter(
        session=db_session,
        service="polar",
        operation="checkouts.create",
        payload={...},
        error="Timeout after 30 s",
        attempts=3,
    )
"""

import asyncio
import logging
from collections.abc import Callable, Awaitable
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.webhook_dead_letter import WebhookDeadLetter

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Maximum retry attempts before writing a dead-letter record
MAX_ATTEMPTS = 3
# Base delays in seconds: 5 min (300s), 30 min (1800s), 2h (7200s)
# These match the T-BE-12 requirement: retry at 5 min, 30 min, 2h
RETRY_DELAYS_SECONDS = [300, 1800, 7200]
MAX_ATTEMPTS = 3
# Base delay in seconds; actual delay = BACKOFF_BASE ** attempt  (1 s, 2 s, 4 s)
BACKOFF_BASE = 2.0


async def with_retry(
    coro_fn: Callable[[], Any],
    *,
    service: str,
    operation: str,
    payload: dict[str, Any],
    session: AsyncSession,
    max_attempts: int = MAX_ATTEMPTS,
    backoff_base: float = BACKOFF_BASE,
) -> Any:
    """
    Call *coro_fn()* up to *max_attempts* times with exponential back-off.

    - If all attempts fail, writes a :class:`WebhookDeadLetter` row and
      re-raises the last exception so the caller can surface it to the user.
    - Works with both coroutines (``async def``) and synchronous callables
      (e.g. the sync Polar SDK).

    Parameters
    ----------
    coro_fn:
        Zero-argument callable that returns the result (or a coroutine that
        does).  Called fresh on every attempt.
    service:
        Short service tag for the dead-letter record, e.g. ``"polar"``.
    operation:
        Dotted operation name, e.g. ``"checkouts.create"``.
    payload:
        Serialisable dict that captures the request parameters — stored in
        the dead-letter row for later replay.
    session:
        The active :class:`AsyncSession`.  The dead-letter record is flushed
        (not committed) so it participates in the caller's transaction.
    """
    last_exc: Exception | None = None

    for attempt in range(max_attempts):
        try:
            result = coro_fn()
            # Support both sync return values and awaitables
            if asyncio.iscoroutine(result):
                return await result
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning(
                "[webhook_retry] %s/%s attempt %d/%d failed: %s",
                service,
                operation,
                attempt + 1,
                max_attempts,
                exc,
            )
            if attempt < max_attempts - 1:
                # Use predefined delays: 5 min, 30 min, 2 hours (T-BE-12)
                delay = RETRY_DELAYS_SECONDS[attempt] if attempt < len(RETRY_DELAYS_SECONDS) else RETRY_DELAYS_SECONDS[-1]
                logger.info("[webhook_retry] Waiting %d seconds before retry...", delay)
                await asyncio.sleep(delay)
                delay = backoff_base**attempt
                await asyncio.sleep(delay)

    # All attempts exhausted — write dead-letter record
    assert last_exc is not None  # noqa: S101 — always set after the loop
    await record_dead_letter(
        session=session,
        service=service,
        operation=operation,
        payload=payload,
        error=str(last_exc),
        attempts=max_attempts,
    )
    raise last_exc


async def record_dead_letter(
    *,
    session: AsyncSession,
    service: str,
    operation: str,
    payload: dict[str, Any],
    error: str,
    attempts: int,
) -> WebhookDeadLetter:
    """
    Persist a :class:`WebhookDeadLetter` row directly (no retry).

    Useful when the caller has already handled retries itself and just needs
    to record the permanent failure (e.g. email queue at retry_count >= 3).

    The record is *flushed* but not *committed* — it participates in whatever
    transaction is currently active on *session*.
    """
    record = WebhookDeadLetter(
        service=service,
        operation=operation,
        payload=payload,
        error_message=error[:4096],  # guard against unbounded TEXT
        attempts=attempts,
        resolved=False,
    )
    session.add(record)
    await session.flush()
    logger.error(
        "[dead-letter] %s/%s failed permanently after %d attempt(s): %s",
        service,
        operation,
        attempts,
        error,
    )
    return record
