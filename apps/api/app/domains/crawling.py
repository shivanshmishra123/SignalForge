from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from fastapi import HTTPException, status

from app.domains.async_utils import maybe_await
from app.domains.content import normalize_content
from app.domains.events import (
    EventRepository,
    FakeLLMProvider,
    LLMProvider,
    classify_event,
    create_candidate_events,
)
from app.domains.monitoring import MonitoringRepository, SourceStatus
from app.providers.crawler import Crawler, CrawlError, FetchedDocument, PlaywrightCrawler


class CrawlRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL_FAILED = "partial_failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class CrawlRun:
    id: str
    workspace_id: str
    source_id: str
    idempotency_key: str
    status: CrawlRunStatus
    started_at: datetime
    finished_at: datetime | None = None
    error_code: str | None = None
    retryable: bool = False
    attempt: int = 0
    duration_ms: int | None = None
    byte_count: int = 0


@dataclass
class SourceSnapshot:
    id: str
    workspace_id: str
    source_id: str
    run_id: str
    fetched_url: str
    content_type: str
    content: str
    content_hash: str
    normalized_sections: list[dict[str, str]]
    byte_count: int
    fetched_at: datetime


class CrawlRepository:
    def __init__(self) -> None:
        self.runs: dict[str, CrawlRun] = {}
        self.snapshots: dict[str, SourceSnapshot] = {}

    def find_run(
        self, source_id: str, idempotency_key: str, workspace_id: str | None = None
    ) -> CrawlRun | None:
        return next(
            (
                run
                for run in self.runs.values()
                if run.source_id == source_id
                and run.idempotency_key == idempotency_key
                and (workspace_id is None or run.workspace_id == workspace_id)
            ),
            None,
        )

    def save_run(self, run: CrawlRun) -> CrawlRun:
        self.runs[run.id] = run
        return run

    def save_snapshot(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        existing = next(
            (
                item
                for item in self.snapshots.values()
                if item.source_id == snapshot.source_id
                and item.content_hash == snapshot.content_hash
            ),
            None,
        )
        if existing is not None:
            return existing
        self.snapshots[snapshot.id] = snapshot
        return snapshot

    def get_run(self, run_id: str, workspace_id: str | None = None) -> CrawlRun | None:
        run = self.runs.get(run_id)
        if run is None or (workspace_id is not None and run.workspace_id != workspace_id):
            return None
        return run

    def list_runs(self, workspace_id: str) -> list[CrawlRun]:
        return [run for run in self.runs.values() if run.workspace_id == workspace_id]

    def latest_snapshot(self, source_id: str) -> SourceSnapshot | None:
        snapshots = [item for item in self.snapshots.values() if item.source_id == source_id]
        return max(snapshots, key=lambda item: item.fetched_at, default=None)

    def get_snapshot(
        self, snapshot_id: str, workspace_id: str | None = None
    ) -> SourceSnapshot | None:
        snapshot = self.snapshots.get(snapshot_id)
        if snapshot is None or (workspace_id is not None and snapshot.workspace_id != workspace_id):
            return None
        return snapshot


class CrawlService:
    def __init__(
        self,
        monitoring: MonitoringRepository,
        repository: CrawlRepository,
        crawler: Crawler,
        playwright_crawler: Crawler | None = None,
        event_repository: EventRepository | None = None,
        classifier: LLMProvider | None = None,
        classifier_model: str = "fake-v1",
    ) -> None:
        self.monitoring = monitoring
        self.repository = repository
        self.crawler = crawler
        self.playwright_crawler = playwright_crawler or PlaywrightCrawler()
        self.event_repository = event_repository
        self.classifier = classifier or FakeLLMProvider()
        self.classifier_model = classifier_model

    async def crawl_source(
        self, workspace_id: str, source_id: str, idempotency_key: str
    ) -> CrawlRun:
        source = await maybe_await(self.monitoring.get_source(workspace_id, source_id))
        existing = await maybe_await(
            self.repository.find_run(source_id, idempotency_key, workspace_id)
        )
        if existing is not None:
            return existing
        if source.status is not SourceStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "SOURCE_NOT_ACTIVE",
                    "message": "Only active sources can be crawled.",
                },
            )
        run = await maybe_await(
            self.repository.save_run(
                CrawlRun(
                    id=str(uuid4()),
                    workspace_id=workspace_id,
                    source_id=source_id,
                    idempotency_key=idempotency_key,
                    status=CrawlRunStatus.RUNNING,
                    started_at=datetime.now(UTC),
                )
            )
        )
        return await self.execute_run(run)

    async def execute_run(self, run: CrawlRun) -> CrawlRun:
        source = await maybe_await(self.monitoring.get_source(run.workspace_id, run.source_id))
        started = datetime.now(UTC)
        try:
            crawler = (
                self.playwright_crawler
                if source.parser_key == "javascript_required"
                else self.crawler
            )
            document = await crawler.fetch(source.url, source.source_type)
            latest_snapshot = getattr(self.repository, "latest_snapshot", None)
            previous = (
                await maybe_await(latest_snapshot(run.source_id))
                if latest_snapshot is not None
                else None
            )
            snapshot = await maybe_await(
                self.repository.save_snapshot(
                    self._snapshot(run.workspace_id, run.source_id, run.id, document)
                )
            )
            if self.event_repository is not None:
                candidates = await create_candidate_events(
                    self.event_repository, previous, snapshot
                )
                for event in candidates:
                    await classify_event(
                        self.event_repository,
                        event,
                        previous,
                        snapshot,
                        self.classifier,
                        self.classifier_model,
                    )
        except CrawlError as exc:
            run.status = CrawlRunStatus.FAILED
            run.error_code = exc.code
            run.retryable = exc.retryable
            run.finished_at = datetime.now(UTC)
            # A transient outage must not take an otherwise healthy source out
            # of rotation. Permanent policy/content failures do require review.
            if not exc.retryable:
                source.status = SourceStatus.FAILED
            source.last_failure_code = exc.code
            await maybe_await(self.monitoring.save_source(source))
            run.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            await maybe_await(self.monitoring.save_source(source))
            await maybe_await(self.repository.save_run(run))
            return run
        run.status = CrawlRunStatus.SUCCEEDED
        run.finished_at = datetime.now(UTC)
        source.last_success_at = run.finished_at
        source.last_failure_code = None
        await maybe_await(self.monitoring.save_source(source))
        run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
        run.byte_count = snapshot.byte_count
        await maybe_await(self.monitoring.save_source(source))
        await maybe_await(self.repository.save_run(run))
        return run

    @staticmethod
    def _snapshot(
        workspace_id: str, source_id: str, run_id: str, document: FetchedDocument
    ) -> SourceSnapshot:
        normalized = normalize_content(document.content, document.content_type)
        return SourceSnapshot(
            id=str(uuid4()),
            workspace_id=workspace_id,
            source_id=source_id,
            run_id=run_id,
            fetched_url=document.final_url,
            content_type=document.content_type,
            content=normalized.text,
            content_hash=normalized.content_hash,
            normalized_sections=normalized.serialized_sections,
            byte_count=document.byte_count,
            fetched_at=datetime.fromisoformat(document.fetched_at),
        )
