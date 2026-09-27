from pathlib import Path

import pytest
from app.db.migrations import migration_files
from app.db.session import async_database_url
from app.domains.crawling import CrawlRepository, CrawlRunStatus, CrawlService
from app.domains.monitoring import CompetitorCreate, MonitoringRepository, SourceCreate, SourceType
from app.domains.operations import CrawlOperations
from app.providers.crawler import TransientCrawlError
from app.providers.queue import RedisCrawlQueue, RedisQueueConfigurationError
from fastapi import HTTPException


class FailingCrawler:
    async def fetch(self, url: str, source_type: SourceType) -> None:
        raise TransientCrawlError("fixture outage")


def _operations() -> tuple[MonitoringRepository, CrawlOperations, str]:
    monitoring = MonitoringRepository()
    competitor = monitoring.create_competitor(
        "workspace-a", CompetitorCreate(name="Acme", canonical_domain="acme.example")
    )
    source = monitoring.create_source(
        "workspace-a",
        SourceCreate(
            competitor_id=competitor.id,
            source_type=SourceType.HTML,
            url="https://acme.example/pricing",
        ),
    )
    repository = CrawlRepository()
    service = CrawlService(monitoring, repository, FailingCrawler())
    return monitoring, CrawlOperations(service, repository), source.id


@pytest.mark.anyio
async def test_transient_failure_keeps_source_active() -> None:
    monitoring, operations, source_id = _operations()
    run = await operations.enqueue("workspace-a", source_id, "outage")
    await operations.process_job(await operations.queue.get())

    assert run.status is CrawlRunStatus.FAILED
    assert monitoring.get_source("workspace-a", source_id).status.value == "active"


@pytest.mark.anyio
async def test_exhausted_transient_retries_are_partial_failed() -> None:
    _, operations, source_id = _operations()
    run = await operations.enqueue("workspace-a", source_id, "outage")
    for _ in range(3):
        await operations.process_job(await operations.queue.get())

    assert run.status is CrawlRunStatus.PARTIAL_FAILED
    assert run.retryable is True
    assert run.id in operations.repository.dead_letters


def test_schedule_requires_existing_source_and_bounded_interval() -> None:
    _, operations, source_id = _operations()

    with pytest.raises(HTTPException) as missing:
        operations.create_schedule("workspace-a", "missing", 60)
    assert missing.value.status_code == 404
    assert missing.value.detail["code"] == "SOURCE_NOT_FOUND"

    with pytest.raises(HTTPException) as invalid:
        operations.create_schedule("workspace-a", source_id, 1)
    assert invalid.value.status_code == 422
    assert invalid.value.detail["code"] == "INVALID_SCHEDULE_INTERVAL"


def test_database_url_is_normalized_for_asyncpg() -> None:
    assert async_database_url("postgresql://db/signalforge") == (
        "postgresql+asyncpg://db/signalforge"
    )
    assert async_database_url("postgresql+asyncpg://db/signalforge") == (
        "postgresql+asyncpg://db/signalforge"
    )


def test_migrations_are_lexically_ordered_and_dependency_safe() -> None:
    migrations = migration_files(Path("migrations"))
    assert [migration.name for migration in migrations][:2] == [
        "001_identity_and_tenant_boundaries.sql",
        "002_competitors_and_sources.sql",
    ]
    assert "workspaces" in migrations[0].read_text(encoding="utf-8")
    assert "competitors" in migrations[1].read_text(encoding="utf-8")


def test_redis_backend_requires_explicit_configuration() -> None:
    with pytest.raises(RedisQueueConfigurationError, match="REDIS_URL"):
        RedisCrawlQueue("")
