"""Simple in-process token-bucket rate limiter.

Uses an in-memory store by default so it works without Redis.
When Redis is configured the same store is still used per-process — for
multi-replica deployments, wire up a Redis-backed implementation instead.

Rate-limit keys are the authenticated user_id when available, falling back to
the client IP address (X-Forwarded-For respected for Render's proxy).
"""

import time
from collections import defaultdict
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class _Bucket:
    """Token-bucket per subject."""

    __slots__ = ("tokens", "last_refill")

    def __init__(self, capacity: float) -> None:
        self.tokens: float = capacity
        self.last_refill: float = time.monotonic()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Token-bucket rate limiter.

    Parameters
    ----------
    requests_per_minute:
        How many requests a single subject may make per 60 seconds.
    burst:
        Maximum token accumulation (defaults to requests_per_minute).
    exempt_paths:
        Paths that bypass rate limiting entirely (e.g. ``/health``).
    """

    def __init__(
        self,
        app,
        requests_per_minute: int = 60,
        burst: int | None = None,
        exempt_paths: frozenset[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._rpm = requests_per_minute
        self._refill_rate = requests_per_minute / 60.0  # tokens per second
        self._burst = burst if burst is not None else requests_per_minute
        self._buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(self._burst))
        self._lock = Lock()
        self._exempt = exempt_paths or frozenset({"/health", "/ready", "/metrics"})

    def _subject(self, request: Request) -> str:
        # Prefer authenticated user_id set by auth middleware
        user_id = getattr(getattr(request.state, "principal", None), "user_id", None)
        if user_id:
            return f"user:{user_id}"
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"

    def _consume(self, subject: str) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(subject)
            if bucket is None:
                bucket = _Bucket(self._burst)
                self._buckets[subject] = bucket
            else:
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._refill_rate)
                bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            retry_after = (1.0 - bucket.tokens) / self._refill_rate
            return False, retry_after

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS" or request.url.path in self._exempt:
            return await call_next(request)

        subject = self._subject(request)
        allowed, retry_after = self._consume(subject)
        if not allowed:
            return Response(
                content=(
                    '{"detail":{"code":"RATE_LIMITED",'
                    '"message":"Too many requests, please slow down."}}'
                ),
                status_code=429,
                media_type="application/json",
                headers={
                    "Retry-After": str(int(retry_after) + 1),
                    "X-RateLimit-Limit": str(self._rpm),
                },
            )
        response = await call_next(request)
        return response
