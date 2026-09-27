import os
from datetime import UTC, datetime

import pytest
from app.db.migrations import run_migrations
from app.db.session import create_engine
from app.domains.operations import CrawlJob
from app.providers.queue import RedisCrawlQueue
from app.settings import Settings
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION_TESTS") != "1",
    reason="Docker-backed integration tests are enabled explicitly in CI.",
)


@pytest.mark.anyio
async def test_postgres_migrations_are_idempotent_and_create_event_tables() -> None:
    settings = Settings(
        database_url=os.environ["DATABASE_URL"],
        repository_backend="postgres",
    )
    engine = create_engine(settings)
    assert engine is not None
    try:
        await run_migrations(engine, "migrations")
        second = await run_migrations(engine, "migrations")
        assert second == []
        async with engine.connect() as connection:
            tables = {
                row[0]
                for row in (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                ).all()
            }
        assert {"source_snapshots", "change_events", "evidence_spans"} <= tables
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_redis_queue_round_trip() -> None:
    queue = RedisCrawlQueue(os.environ["REDIS_URL"], key="signalforge:test-jobs")
    job = CrawlJob(
        run_id="integration-run",
        workspace_id="integration-workspace",
        source_id="integration-source",
        idempotency_key="integration-key",
        next_attempt_at=datetime.now(UTC),
    )
    try:
        await queue._client.delete(queue.key)
        await queue.enqueue(job)
        received = await queue.get()
        assert received == job
    finally:
        await queue._client.delete(queue.key)
        await queue.close()
