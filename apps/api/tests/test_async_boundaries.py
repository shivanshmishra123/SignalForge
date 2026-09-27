import httpx
import pytest
from app.domains.crawling import CrawlRepository, CrawlRunStatus, CrawlService
from app.domains.monitoring import (
    CompetitorCreate,
    MonitoringRepository,
    SourceCreate,
    SourceType,
)
from app.domains.operations import CrawlOperations, OperationsRepository
from app.providers.crawler import DomainRateLimiter, HttpCrawler, RobotsPolicy


class AsyncMonitoring:
    def __init__(self) -> None:
        self.inner = MonitoringRepository()

    async def create_competitor(self, workspace_id, data):
        return self.inner.create_competitor(workspace_id, data)

    async def create_source(self, workspace_id, data):
        return self.inner.create_source(workspace_id, data)

    async def get_source(self, workspace_id, source_id):
        return self.inner.get_source(workspace_id, source_id)

    async def save_source(self, source):
        return self.inner.save_source(source)


class AsyncCrawl:
    def __init__(self) -> None:
        self.inner = CrawlRepository()

    async def find_run(self, source_id, idempotency_key, workspace_id=None):
        return self.inner.find_run(source_id, idempotency_key, workspace_id)

    async def save_run(self, run):
        return self.inner.save_run(run)

    async def save_snapshot(self, snapshot):
        return self.inner.save_snapshot(snapshot)

    async def get_run(self, run_id, workspace_id=None):
        return self.inner.get_run(run_id, workspace_id)


class AsyncOperationsRepository(OperationsRepository):
    async def save_schedule(self, schedule):
        return super().save_schedule(schedule)

    async def save_job(self, job):
        return super().save_job(job)


@pytest.mark.anyio
async def test_crawl_service_supports_async_repositories_and_persists_status() -> None:
    monitoring = AsyncMonitoring()
    competitor = await monitoring.create_competitor(
        "workspace-a", CompetitorCreate(name="Acme", canonical_domain="acme.example")
    )
    source = await monitoring.create_source(
        "workspace-a",
        SourceCreate(
            competitor_id=competitor.id,
            source_type=SourceType.HTML,
            url="https://acme.example/pricing",
        ),
    )

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"ok",
            request=request,
        )

    crawler = HttpCrawler(
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        robots=RobotsPolicy(),
        rate_limiter=DomainRateLimiter(0),
    )
    repository = AsyncCrawl()
    service = CrawlService(monitoring, repository, crawler)

    run = await service.crawl_source("workspace-a", source.id, "async-run")

    assert run.status is CrawlRunStatus.SUCCEEDED
    assert (await repository.get_run(run.id, "workspace-a")).status is CrawlRunStatus.SUCCEEDED
    assert (await monitoring.get_source("workspace-a", source.id)).last_success_at is not None


@pytest.mark.anyio
async def test_operations_worker_accepts_async_schedule_and_job_repository() -> None:
    monitoring = AsyncMonitoring()
    competitor = await monitoring.create_competitor(
        "workspace-a", CompetitorCreate(name="Acme", canonical_domain="acme.example")
    )
    source = await monitoring.create_source(
        "workspace-a",
        SourceCreate(
            competitor_id=competitor.id,
            source_type=SourceType.HTML,
            url="https://acme.example/pricing",
        ),
    )
    crawl = AsyncCrawl()
    crawler = HttpCrawler(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
        ),
        robots=RobotsPolicy(),
        rate_limiter=DomainRateLimiter(0),
    )
    service = CrawlService(monitoring, crawl, crawler)
    operations = CrawlOperations(service, crawl, AsyncOperationsRepository())

    schedule = await operations.create_schedule_async("workspace-a", source.id, 60)
    run = await operations.enqueue("workspace-a", source.id, "async-job")

    assert schedule.source_id == source.id
    assert (await crawl.get_run(run.id, "workspace-a")).status is CrawlRunStatus.QUEUED
