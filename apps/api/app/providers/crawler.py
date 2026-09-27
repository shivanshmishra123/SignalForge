import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Protocol
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.domains.monitoring import SourceType


class CrawlError(Exception):
    """Base class for expected crawl failures."""

    code = "CRAWL_FAILED"
    retryable = False


class TransientCrawlError(CrawlError):
    code = "CRAWL_TRANSIENT"
    retryable = True


class PermanentCrawlError(CrawlError):
    code = "CRAWL_PERMANENT"


class PolicyBlockedError(PermanentCrawlError):
    code = "ROBOTS_BLOCKED"


@dataclass(frozen=True)
class CrawlPolicy:
    user_agent: str = "SignalForgeBot/0.1 (+https://signalforge.local/bot)"
    timeout_seconds: float = 15.0
    max_bytes: int = 2_000_000
    max_redirects: int = 5
    min_interval_seconds: float = 1.0
    allow_private_networks: bool = False


def is_safe_crawl_target(url: str, allow_private: bool = False) -> tuple[bool, str]:
    """Validate that target URL does not target loopback, private, or cloud metadata IPs."""
    if allow_private:
        return True, ""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False, "Missing hostname in target URL."
        hostname = hostname.lower()
        if (
            hostname in {"localhost", "metadata.google.internal"}
            or hostname.endswith(".local")
            or hostname.endswith(".internal")
        ):
            return False, f"Access to private/internal domain '{hostname}' is forbidden."
        try:
            ip = ipaddress.ip_address(hostname)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False, f"Access to private/internal IP '{hostname}' is forbidden."
            return True, ""
        except ValueError:
            pass
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            for item in addr_info:
                ip_str = item[4][0]
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_reserved
                    or ip.is_multicast
                ):
                    return False, f"Domain '{hostname}' resolves to private/internal IP '{ip_str}'."
        except (socket.gaierror, OSError):
            pass
        return True, ""
    except Exception as exc:
        return False, f"Invalid URL for crawl target: {exc}"


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    content: str
    fetched_at: str
    byte_count: int


class DomainRateLimiter:
    def __init__(self, min_interval_seconds: float = 1.0) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._last_request: dict[str, float] = {}

    async def wait(self, url: str) -> None:
        domain = urlparse(url).netloc.lower()
        now = monotonic()
        previous = self._last_request.get(domain)
        if previous is not None:
            delay = self.min_interval_seconds - (now - previous)
            if delay > 0:
                import asyncio

                await asyncio.sleep(delay)
        self._last_request[domain] = monotonic()


class RobotsPolicy:
    def __init__(
        self,
        loader: Callable[[str], Awaitable[str]] | None = None,
        user_agent: str = "SignalForgeBot/0.1",
    ) -> None:
        self.loader = loader
        self.user_agent = user_agent
        self._parsers: dict[str, RobotFileParser] = {}

    async def allowed(self, url: str) -> bool:
        domain = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if domain not in self._parsers:
            if self.loader is None:
                return True
            robots_text = await self.loader(f"{domain}/robots.txt")
            parser = RobotFileParser()
            parser.parse(robots_text.splitlines())
            self._parsers[domain] = parser
        return self._parsers[domain].can_fetch(self.user_agent, url)


class Crawler(Protocol):
    async def fetch(self, url: str, source_type: SourceType) -> FetchedDocument:
        """Fetch and validate one configured public source."""


class HttpCrawler:
    def __init__(
        self,
        policy: CrawlPolicy | None = None,
        client: httpx.AsyncClient | None = None,
        robots: RobotsPolicy | None = None,
        rate_limiter: DomainRateLimiter | None = None,
    ) -> None:
        self.policy = policy or CrawlPolicy()
        self.client = client
        self.robots = robots or RobotsPolicy(user_agent=self.policy.user_agent)
        self.rate_limiter = rate_limiter or DomainRateLimiter(self.policy.min_interval_seconds)

    async def fetch(self, url: str, source_type: SourceType) -> FetchedDocument:
        safe, reason = is_safe_crawl_target(url, self.policy.allow_private_networks)
        if not safe:
            raise PolicyBlockedError(f"SSRF_BLOCKED: {reason}")
        if not await self.robots.allowed(url):
            raise PolicyBlockedError("robots.txt disallows this URL")
        await self.rate_limiter.wait(url)
        close_client = self.client is None
        client = self.client or httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=self.policy.max_redirects,
            timeout=self.policy.timeout_seconds,
            headers={"User-Agent": self.policy.user_agent},
        )
        try:
            try:
                response = await client.get(url)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise TransientCrawlError(str(exc)) from exc
            if response.status_code == 429 or response.status_code >= 500:
                raise TransientCrawlError(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                raise PermanentCrawlError(f"HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "").split(";")[0].lower()
            if len(response.content) > self.policy.max_bytes:
                raise PermanentCrawlError("response exceeded configured size limit")
            content = response.text
            if source_type is SourceType.HTML:
                if "html" not in content_type and "text/plain" not in content_type:
                    raise PermanentCrawlError("source did not return HTML content")
                content = str(BeautifulSoup(content, "html.parser"))
            elif source_type is SourceType.RSS and not (
                "xml" in content_type or "rss" in content_type or "<rss" in content.lower()
            ):
                raise PermanentCrawlError("source did not return RSS/XML content")
            return FetchedDocument(
                requested_url=url,
                final_url=str(response.url),
                status_code=response.status_code,
                content_type=content_type,
                content=content,
                fetched_at=parsedate_to_datetime(response.headers["date"]).isoformat()
                if response.headers.get("date")
                else "1970-01-01T00:00:00+00:00",
                byte_count=len(response.content),
            )
        finally:
            if close_client:
                await client.aclose()


class PlaywrightCrawler:
    async def fetch(self, url: str, source_type: SourceType) -> FetchedDocument:
        raise PermanentCrawlError(
            "Playwright adapter is available only when the optional browser runtime is configured."
        )
