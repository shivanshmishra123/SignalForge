import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException, status

from app.domains.async_utils import maybe_await
from app.domains.crawling import CrawlRepository, CrawlRun, CrawlRunStatus, CrawlService

MIN_SCHEDULE_INTERVAL_MINUTES = 15
MAX_SCHEDULE_INTERVAL_MINUTES = 10080


@dataclass
class CrawlSchedule:
    id: str
    workspace_id: str
    source_id: str
    interval_minutes: int
    active: bool = True
    next_run_at: datetime | None = None


@dataclass
class CrawlJob:
    run_id: str
    workspace_id: str
    source_id: str
    idempotency_key: str
    attempt: int = 0
    max_attempts: int = 3
    next_attempt_at: datetime | None = None


class OperationsRepository:
    def __init__(self) -> None:
        self.schedules: dict[str, CrawlSchedule] = {}
        self.jobs: dict[str, CrawlJob] = {}
        self.dead_letters: dict[str, CrawlJob] = {}
        self.locks: set[str] = set()

    def save_schedule(self, schedule: CrawlSchedule) -> CrawlSchedule:
        self.schedules[schedule.id] = schedule
        return schedule

    def save_job(self, job: CrawlJob) -> CrawlJob:
        self.jobs[job.run_id] = job
        return job


class CrawlQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[CrawlJob] = asyncio.Queue()

    async def enqueue(self, job: CrawlJob) -> None:
        await self._queue.put(job)

    async def get(self) -> CrawlJob:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def size(self) -> int:
        return self._queue.qsize()


class CrawlOperations:
    def __init__(
        self,
        crawl_service: CrawlService,
        crawl_repository: CrawlRepository,
        repository: OperationsRepository | None = None,
        queue: CrawlQueue | None = None,
    ) -> None:
        self.crawl_service = crawl_service
        self.crawl_repository = crawl_repository
        self.repository = repository or OperationsRepository()
        self.queue = queue or CrawlQueue()
        self._worker_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def create_schedule(
        self, workspace_id: str, source_id: str, interval_minutes: int
    ) -> CrawlSchedule:
        if not MIN_SCHEDULE_INTERVAL_MINUTES <= interval_minutes <= MAX_SCHEDULE_INTERVAL_MINUTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "INVALID_SCHEDULE_INTERVAL",
                    "message": (
                        f"Schedule interval must be between {MIN_SCHEDULE_INTERVAL_MINUTES} "
                        f"and {MAX_SCHEDULE_INTERVAL_MINUTES} minutes."
                    ),
                },
            )
        try:
            source = self.crawl_service.monitoring.get_source(workspace_id, source_id)
        except HTTPException:
            raise
        if source.status.value != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "SOURCE_NOT_ACTIVE",
                    "message": "Only active sources can be scheduled.",
                },
            )
        schedule = CrawlSchedule(
            id=str(uuid4()),
            workspace_id=workspace_id,
            source_id=source_id,
            interval_minutes=interval_minutes,
            next_run_at=datetime.now(UTC) + timedelta(minutes=interval_minutes),
        )
        return self.repository.save_schedule(schedule)

    async def create_schedule_async(
        self, workspace_id: str, source_id: str, interval_minutes: int
    ) -> CrawlSchedule:
        if not MIN_SCHEDULE_INTERVAL_MINUTES <= interval_minutes <= MAX_SCHEDULE_INTERVAL_MINUTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "INVALID_SCHEDULE_INTERVAL",
                    "message": (
                        f"Schedule interval must be between {MIN_SCHEDULE_INTERVAL_MINUTES} "
                        f"and {MAX_SCHEDULE_INTERVAL_MINUTES} minutes."
                    ),
                },
            )
        source = await maybe_await(
            self.crawl_service.monitoring.get_source(workspace_id, source_id)
        )
        if source.status.value != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "SOURCE_NOT_ACTIVE",
                    "message": "Only active sources can be scheduled.",
                },
            )
        schedule = CrawlSchedule(
            id=str(uuid4()),
            workspace_id=workspace_id,
            source_id=source_id,
            interval_minutes=interval_minutes,
            next_run_at=datetime.now(UTC) + timedelta(minutes=interval_minutes),
        )
        return await maybe_await(self.repository.save_schedule(schedule))

    async def enqueue(self, workspace_id: str, source_id: str, idempotency_key: str) -> CrawlRun:
        source = await maybe_await(
            self.crawl_service.monitoring.get_source(workspace_id, source_id)
        )
        if source.status.value != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "SOURCE_NOT_ACTIVE",
                    "message": "Only active sources can be crawled.",
                },
            )
        existing = await maybe_await(
            self.crawl_repository.find_run(source_id, idempotency_key, workspace_id)
        )
        if existing is not None:
            return existing
        run = CrawlRun(
            id=str(uuid4()),
            workspace_id=workspace_id,
            source_id=source_id,
            idempotency_key=idempotency_key,
            status=CrawlRunStatus.QUEUED,
            started_at=datetime.now(UTC),
        )
        await maybe_await(self.crawl_repository.save_run(run))
        job = CrawlJob(
            run_id=run.id,
            workspace_id=workspace_id,
            source_id=source_id,
            idempotency_key=idempotency_key,
        )
        await maybe_await(self.repository.save_job(job))
        await self.queue.enqueue(job)
        return run

    async def start_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stop_event = asyncio.Event()
            self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop_worker(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._worker_task is not None:
            await self._worker_task
        self._worker_task = None

    async def _worker_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                job = await asyncio.wait_for(self.queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            try:
                await self.process_job(job)
            finally:
                self.queue.task_done()

    async def process_job(self, job: CrawlJob) -> CrawlRun:
        queue_lock = getattr(self.queue, "acquire_lock", None)
        if queue_lock is not None:
            acquired = await queue_lock(job.run_id)
        else:
            acquired = job.run_id not in self.repository.locks
            if acquired:
                self.repository.locks.add(job.run_id)
        if not acquired:
            return await maybe_await(self.crawl_repository.get_run(job.run_id, job.workspace_id))
        try:
            run = await maybe_await(self.crawl_repository.get_run(job.run_id, job.workspace_id))
            if run is None:
                raise KeyError(f"Crawl run {job.run_id} was not found")
            run.status = CrawlRunStatus.RUNNING
            run.attempt = job.attempt + 1
            await maybe_await(self.repository.save_job(job))
            await maybe_await(self.crawl_repository.save_run(run))
            started = datetime.now(UTC)
            result = await self.crawl_service.execute_run(run)
            result.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            await maybe_await(self.crawl_repository.save_run(result))
            if result.status is CrawlRunStatus.FAILED and result.retryable:
                if job.attempt + 1 < job.max_attempts:
                    job.attempt += 1
                    job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=2**job.attempt)
                    await self.queue.enqueue(job)
                else:
                    result.status = CrawlRunStatus.PARTIAL_FAILED
                    self.repository.dead_letters[job.run_id] = job
                    await maybe_await(self.crawl_repository.save_run(result))
            return result
        finally:
            release_lock = getattr(self.queue, "release_lock", None)
            if release_lock is not None:
                await release_lock(job.run_id)
            else:
                self.repository.locks.discard(job.run_id)
