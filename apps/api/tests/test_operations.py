import httpx
import pytest
from app.domains.crawling import CrawlRepository, CrawlRunStatus, CrawlService
from app.domains.monitoring import (
    CompetitorCreate,
    MonitoringRepository,
    SourceCreate,
    SourceType,
)
from app.domains.operations import CrawlJob, CrawlOperations
from app.providers.crawler import DomainRateLimiter, HttpCrawler, RobotsPolicy


def _service() -> tuple[MonitoringRepository, CrawlOperations, str]:
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

    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body><h1>Pricing</h1></body></html>",
            request=request,
        )

    crawler = HttpCrawler(
        client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
        robots=RobotsPolicy(),
        rate_limiter=DomainRateLimiter(0),
    )
    crawl_repository = CrawlRepository()
    service = CrawlService(monitoring, crawl_repository, crawler)
    return monitoring, CrawlOperations(service, crawl_repository), source.id


@pytest.mark.anyio
async def test_enqueue_is_non_blocking_and_worker_processes_once() -> None:
    _, operations, source_id = _service()

    queued = await operations.enqueue("workspace-a", source_id, "scheduled-1")
    assert queued.status is CrawlRunStatus.QUEUED
    assert operations.queue.size == 1

    await operations.process_job(await operations.queue.get())

    assert operations.crawl_repository.runs[queued.id].status is CrawlRunStatus.SUCCEEDED
    assert operations.queue.size == 0


@pytest.mark.anyio
async def test_duplicate_enqueue_returns_same_run_and_lock_prevents_duplicate_work() -> None:
    _, operations, source_id = _service()

    first = await operations.enqueue("workspace-a", source_id, "same-key")
    second = await operations.enqueue("workspace-a", source_id, "same-key")

    assert second.id == first.id
    assert operations.queue.size == 1

    operations.repository.locks.add(first.id)
    result = await operations.process_job(CrawlJob(first.id, "workspace-a", source_id, "same-key"))
    operations.repository.locks.remove(first.id)

    assert result.id == first.id
    assert result.status is CrawlRunStatus.QUEUED
