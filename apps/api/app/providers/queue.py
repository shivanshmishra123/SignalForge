import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

from app.domains.operations import CrawlJob, CrawlQueue


class RedisQueueConfigurationError(RuntimeError):
    """Raised when Redis queueing was requested but cannot be initialized."""


class RedisCrawlQueue:
    """Redis-backed queue adapter for multi-worker deployments.

    Redis is intentionally optional for deterministic local development. This adapter
    does not hide a missing client or connection: callers receive an explicit error
    and can choose the in-memory queue instead.
    """

    def __init__(self, redis_url: str, key: str = "signalforge:crawl-jobs") -> None:
        if not redis_url:
            raise RedisQueueConfigurationError(
                "REDIS_URL is required when the Redis queue backend is selected."
            )
        try:
            from redis import asyncio as redis_asyncio
        except ImportError as exc:
            raise RedisQueueConfigurationError(
                "The redis package is required for the Redis queue backend."
            ) from exc
        self._client = redis_asyncio.from_url(redis_url, decode_responses=True)
        self.key = key
        self.lock_key = f"{key}:locks"

    async def enqueue(self, job: CrawlJob) -> None:
        await self._client.rpush(self.key, json.dumps(asdict(job), default=_json_default))

    async def get(self) -> CrawlJob:
        item = await self._client.blpop(self.key, timeout=1)
        if item is None:
            raise TimeoutError
        _key, payload = item
        values: dict[str, Any] = json.loads(payload)
        if values.get("next_attempt_at"):
            values["next_attempt_at"] = datetime.fromisoformat(values["next_attempt_at"])
        return CrawlJob(**values)

    def task_done(self) -> None:
        # Redis lists remove the item atomically in BLPOP; no local task counter exists.
        return None

    async def acquire_lock(self, run_id: str) -> bool:
        return bool(await self._client.set(f"{self.lock_key}:{run_id}", "1", nx=True, ex=300))

    async def release_lock(self, run_id: str) -> None:
        await self._client.delete(f"{self.lock_key}:{run_id}")

    async def size_async(self) -> int:
        return int(await self._client.llen(self.key))

    async def close(self) -> None:
        await self._client.aclose()


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"Cannot serialize queue value of type {type(value).__name__}")


def create_crawl_queue(
    backend: str = "memory", redis_url: str = ""
) -> CrawlQueue | RedisCrawlQueue:
    if backend == "memory":
        return CrawlQueue()
    if backend == "redis":
        return RedisCrawlQueue(redis_url)
    raise RedisQueueConfigurationError(
        f"Unsupported crawl queue backend: {backend}. Use memory or redis."
    )


__all__ = [
    "CrawlQueue",
    "RedisCrawlQueue",
    "RedisQueueConfigurationError",
    "create_crawl_queue",
]
