import pytest
from app.domains.monitoring import SourceType
from app.providers.crawler import (
    CrawlPolicy,
    HttpCrawler,
    PolicyBlockedError,
    is_safe_crawl_target,
)


def test_ssrf_blocks_loopback_and_metadata_targets():
    blocked_urls = [
        "http://localhost:8080/secret",
        "http://127.0.0.1:8000/ready",
        "http://127.0.0.2:9000/info",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.0.0.5/internal",
        "http://172.16.0.10/admin",
        "http://192.168.1.100/status",
        "http://service.local/api",
        "http://app.internal/metrics",
    ]

    for url in blocked_urls:
        safe, reason = is_safe_crawl_target(url, allow_private=False)
        assert not safe, f"Expected {url} to be blocked by SSRF check"
        assert len(reason) > 0


def test_ssrf_permits_private_when_explicitly_configured():
    # Development fixture server on localhost:9000 is allowed when allow_private=True
    safe, reason = is_safe_crawl_target("http://localhost:9000/competitor.html", allow_private=True)
    assert safe
    assert reason == ""


def test_ssrf_permits_public_domains():
    safe, reason = is_safe_crawl_target("https://example.com/pricing", allow_private=False)
    assert safe
    assert reason == ""


@pytest.mark.anyio
async def test_http_crawler_raises_policy_blocked_on_ssrf():
    crawler = HttpCrawler(policy=CrawlPolicy(allow_private_networks=False))

    with pytest.raises(PolicyBlockedError) as exc:
        await crawler.fetch("http://127.0.0.1:8000/secret", SourceType.HTML)

    assert "SSRF_BLOCKED" in str(exc.value)
