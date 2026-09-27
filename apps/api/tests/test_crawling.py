from pathlib import Path

import httpx
import pytest
from app.domains.content import diff_sections, normalize_content
from app.domains.crawling import CrawlRepository, CrawlRunStatus, CrawlService
from app.domains.monitoring import CompetitorCreate, MonitoringRepository, SourceCreate, SourceType
from app.providers.crawler import (
    DomainRateLimiter,
    HttpCrawler,
    PlaywrightCrawler,
    PolicyBlockedError,
    RobotsPolicy,
    TransientCrawlError,
)


def response_for(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/pricing":
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body><h1>Pricing</h1><p>$49</p></body></html>",
            request=request,
        )
    return httpx.Response(404, request=request)


def test_html_normalization_ignores_markup_and_volatile_dates() -> None:
    first = normalize_content(
        "<html><body><h1>Pricing</h1><p>$49</p><p>Updated 2026-01-02</p></body></html>",
        "text/html",
    )
    second = normalize_content(
        "<html><body><h1>Pricing</h1><p>$49</p><p>Updated 2026-02-03</p></body></html>",
        "text/html",
    )

    assert first.content_hash == second.content_hash
    assert first.section_hashes == second.section_hashes


def test_html_section_diff_reports_added_removed_and_modified() -> None:
    before = normalize_content(
        "<h1>Pricing</h1><p>$49</p><h2>Features</h2><p>Alerts</p>", "text/html"
    )
    after = normalize_content(
        "<h1>Pricing</h1><p>$59</p><h2>Integrations</h2><p>Slack</p>", "text/html"
    )

    changes = diff_sections(before, after)

    assert [(change.key, change.change_type) for change in changes] == [
        ("0:pricing", "modified"),
        ("1:features", "removed"),
        ("1:integrations", "added"),
    ]


def test_rss_normalization_uses_entries_as_sections() -> None:
    document = normalize_content(
        "<rss><channel><item><title>Launch</title><description>New API</description>"
        "</item></channel></rss>",
        "application/rss+xml",
    )

    assert document.sections[0].key == "entry:0"
    assert document.sections[0].heading == "Launch"
    assert document.sections[0].text == "New API"


def test_fixture_parsers_produce_stable_sections() -> None:
    html = normalize_content(Path("fixtures/competitor.html").read_text(), "text/html")
    rss = normalize_content(Path("fixtures/competitor.rss").read_text(), "application/rss+xml")

    assert html.sections[0].heading == "Simple pricing"
    assert html.sections[0].text == "Teams plan starts at $49."
    assert rss.sections[0].heading == "New feature"


@pytest.mark.anyio
async def test_http_crawler_fetches_html_with_fake_transport() -> None:
    transport = httpx.MockTransport(response_for)
    async with httpx.AsyncClient(transport=transport) as client:
        crawler = HttpCrawler(
            client=client,
            robots=RobotsPolicy(),
            rate_limiter=DomainRateLimiter(0),
        )
        document = await crawler.fetch("https://acme.example/pricing", SourceType.HTML)

    assert document.status_code == 200
    assert "<h1>Pricing</h1>" in document.content
    assert document.final_url == "https://acme.example/pricing"


@pytest.mark.anyio
async def test_policy_and_retry_failures_are_classified() -> None:
    blocked = HttpCrawler(
        client=httpx.AsyncClient(transport=httpx.MockTransport(response_for)),
        robots=RobotsPolicy(loader=lambda _: _blocked_robots(), user_agent="SignalForgeBot/0.1"),
        rate_limiter=DomainRateLimiter(0),
    )
    with pytest.raises(PolicyBlockedError):
        await blocked.fetch("https://acme.example/pricing", SourceType.HTML)

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    retrying = HttpCrawler(
        client=httpx.AsyncClient(transport=httpx.MockTransport(server_error)),
        robots=RobotsPolicy(),
        rate_limiter=DomainRateLimiter(0),
    )
    with pytest.raises(TransientCrawlError):
        await retrying.fetch("https://acme.example/pricing", SourceType.HTML)


async def _blocked_robots() -> str:
    return "User-agent: SignalForgeBot/0.1\nDisallow: /pricing\n"


@pytest.mark.anyio
async def test_crawl_service_persists_snapshot_and_is_idempotent() -> None:
    monitoring = MonitoringRepository()
    competitor = monitoring.create_competitor(
        "workspace-a",
        CompetitorCreate(name="Acme", canonical_domain="acme.example"),
    )
    source = monitoring.create_source(
        "workspace-a",
        SourceCreate(
            competitor_id=competitor.id,
            source_type=SourceType.HTML,
            url="https://acme.example/pricing",
        ),
    )
    crawler = HttpCrawler(
        client=httpx.AsyncClient(transport=httpx.MockTransport(response_for)),
        robots=RobotsPolicy(),
        rate_limiter=DomainRateLimiter(0),
    )
    repository = CrawlRepository()
    service = CrawlService(monitoring, repository, crawler)

    first = await service.crawl_source("workspace-a", source.id, "run-1")
    second = await service.crawl_source("workspace-a", source.id, "run-1")

    assert first.status is CrawlRunStatus.SUCCEEDED
    assert second.id == first.id
    assert len(repository.runs) == 1
    assert len(repository.snapshots) == 1
    assert repository.snapshots[next(iter(repository.snapshots))].normalized_sections


@pytest.mark.anyio
async def test_javascript_source_uses_explicit_playwright_adapter() -> None:
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
            parser_key="javascript_required",
        ),
    )
    service = CrawlService(
        monitoring,
        CrawlRepository(),
        HttpCrawler(
            client=httpx.AsyncClient(transport=httpx.MockTransport(response_for)),
            robots=RobotsPolicy(),
            rate_limiter=DomainRateLimiter(0),
        ),
        playwright_crawler=PlaywrightCrawler(),
    )

    run = await service.crawl_source("workspace-a", source.id, "javascript-run")

    assert run.status is CrawlRunStatus.FAILED
    assert run.error_code == "CRAWL_PERMANENT"
    assert run.retryable is False


@pytest.mark.anyio
async def test_failed_fetch_does_not_create_snapshot() -> None:
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

    def failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    repository = CrawlRepository()
    service = CrawlService(
        monitoring,
        repository,
        HttpCrawler(
            client=httpx.AsyncClient(transport=httpx.MockTransport(failure)),
            robots=RobotsPolicy(),
            rate_limiter=DomainRateLimiter(0),
        ),
    )

    run = await service.crawl_source("workspace-a", source.id, "failed-run")

    assert run.status is CrawlRunStatus.FAILED
    assert run.error_code == "CRAWL_TRANSIENT"
    assert run.retryable is True
    assert repository.snapshots == {}
