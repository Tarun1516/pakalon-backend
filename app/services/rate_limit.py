"""
Rate limiting service — Redis sliding window counter (T-BE-22).

Limits:
  - Free plan:  60 AI proxy requests / minute
  - Pro plan:  300 AI proxy requests / minute

The sliding window algorithm uses a sorted-set keyed by
``ratelimit:{user_id}:{window_key}`` where every member is a UUID request ID
and the score is the Unix-millisecond timestamp of the request.  Old entries
(older than 60 seconds) are pruned atomically with each check.

Usage:
    ok, remaining, retry_after = await check_rate_limit(redis, user_id, plan)
    if not ok:
        raise HTTPException(429, headers={"Retry-After": str(retry_after)})
"""
from __future__ import annotations

import time
import uuid
from typing import Tuple

WINDOW_SECONDS = 60  # 1-minute rolling window
FREE_LIMIT = 60      # requests per minute — free plan
PRO_LIMIT = 300      # requests per minute — pro plan


def _limit_for_plan(plan: str) -> int:
    """Return the per-minute request limit for the given plan slug."""
    if plan in ("pro", "enterprise"):
        return PRO_LIMIT
    return FREE_LIMIT


async def check_rate_limit(
    redis,
    user_id: str,
    plan: str,
) -> Tuple[bool, int, int]:
    """
    Sliding-window rate limit check using Redis sorted set.

    Args:
        redis:   aioredis client instance (from `app.main.redis_client`).
        user_id: Authenticated user ID string.
        plan:    User plan slug ("free", "pro", "enterprise").

    Returns:
        (allowed, remaining, retry_after_seconds)
        - allowed:       True if the request is within the rate limit.
        - remaining:     How many more requests the user can make this window.
        - retry_after:   Seconds to wait before retrying (0 when allowed).
    """
    limit = _limit_for_plan(plan)
    now_ms = int(time.time() * 1000)
    window_ms = WINDOW_SECONDS * 1000
    window_start_ms = now_ms - window_ms

    key = f"ratelimit:{user_id}:ai"
    request_id = str(uuid.uuid4())

    # Use a pipeline for atomicity
    pipe = redis.pipeline()
    # 1. Remove entries older than the window
    pipe.zremrangebyscore(key, "-inf", window_start_ms)
    # 2. Count current entries in window
    pipe.zcard(key)
    # 3. Add the current request
    pipe.zadd(key, {request_id: now_ms})
    # 4. Set TTL on the key so Redis auto-cleans unused keys
    pipe.expire(key, WINDOW_SECONDS * 2)
    results = await pipe.execute()

    current_count: int = results[1]  # count BEFORE adding this request

    if current_count >= limit:
        # Rate limited — find the oldest entry to compute retry_after
        oldest_entries = await redis.zrange(key, 0, 0, withscores=True)
        if oldest_entries:
            oldest_ms: float = oldest_entries[0][1]
            retry_after = max(1, int((oldest_ms + window_ms - now_ms) / 1000) + 1)
        else:
            retry_after = WINDOW_SECONDS
        remaining = 0
        # Remove the speculative entry we just added (we're denying the request)
        await redis.zrem(key, request_id)
        return False, remaining, retry_after

    remaining = max(0, limit - current_count - 1)
    return True, remaining, 0


def rate_limit_headers(remaining: int, limit: int, retry_after: int = 0) -> dict[str, str]:
    """Build RFC-compliant rate-limit response headers."""
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Window": str(WINDOW_SECONDS),
    }
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return headers
