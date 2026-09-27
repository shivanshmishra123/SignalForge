from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, FastAPI, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.auth.authorization import require_roles, workspace_auth_dependency
from app.auth.models import AuthPrincipal, WorkspaceRole
from app.db.audit import AuditRecord, build_audit_record
from app.db.migrations import run_migrations
from app.db.repositories import (
    PostgresAuditRepository,
    PostgresBriefingRepository,
    PostgresCrawlRepository,
    PostgresEventRepository,
    PostgresMonitoringRepository,
    PostgresOperationsRepository,
)
from app.db.session import check_database, dispose_engine, session_factory
from app.domains.async_utils import maybe_await
from app.domains.briefings import (
    Briefing,
    FakeSlackProvider,
    MemoryBriefingRepository,
    SlackDestination,
    SlackIntegration,
    generate_briefing,
    publish_briefing,
    weekly_period,
)
from app.domains.crawling import CrawlRepository, CrawlService
from app.domains.events import (
    EventCategory,
    EventStatus,
    EventVersionConflict,
    FakeLLMProvider,
    ImpactLevel,
    MemoryEventRepository,
    review_event,
)
from app.domains.monitoring import (
    CompetitorCreate,
    CompetitorResponse,
    MonitoringRepository,
    SourceCreate,
    SourceResponse,
    SourceUpdate,
    competitor_response,
    source_response,
)
from app.domains.operations import CrawlOperations
from app.logging_config import CorrelationMiddleware, configure_logging
from app.metrics import get_metrics_collector
from app.providers.crawler import CrawlPolicy, HttpCrawler, PlaywrightCrawler
from app.providers.gemini import GeminiLLMProvider
from app.providers.queue import create_crawl_queue
from app.providers.slack import DisabledSlackProvider, SlackApiProvider
from app.rate_limit import RateLimitMiddleware
from app.scheduler import CrawlScheduler
from app.settings import Settings, get_settings, secret_value, validate_startup_settings

workspace_access_dependency = workspace_auth_dependency()
admin_access_dependency = require_roles(WorkspaceRole.OWNER, WorkspaceRole.ADMIN)
review_access_dependency = require_roles(
    WorkspaceRole.OWNER, WorkspaceRole.ADMIN, WorkspaceRole.ANALYST
)
workspace_access_parameter = Depends(workspace_access_dependency)
admin_access_parameter = Depends(admin_access_dependency)
review_access_parameter = Depends(review_access_dependency)


class ReviewRequest(BaseModel):
    action: Literal["approve", "reject", "edit_classification", "add_note"]
    expected_version: int | None = Field(default=None, ge=0)
    version: int | None = Field(default=None, ge=0)
    category: EventCategory | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    impact: ImpactLevel | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    note: str | None = Field(default=None, max_length=4000)


class BriefingPreviewRequest(BaseModel):
    period_start: date | None = None
    period_end: date | None = None
    dashboard_url: str = "http://localhost:5173"


class SlackConnectRequest(BaseModel):
    team_id: str = Field(min_length=1, max_length=255)
    token_reference: str = Field(min_length=1, max_length=255)


class SlackDestinationRequest(BaseModel):
    channel_id: str = Field(min_length=1, max_length=255)
    channel_name: str | None = Field(default=None, max_length=255)
    enabled: bool = True


class MemoryAuditRepository:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def save(self, record: AuditRecord) -> AuditRecord:
        self.records.append(record)
        return record

    def list_for_entity(self, workspace_id: str, entity_id: str) -> list[AuditRecord]:
        return [
            item
            for item in self.records
            if item.workspace_id == workspace_id and item.entity_id == entity_id
        ]


@dataclass
class AppRuntime:
    monitoring: Any
    crawl: Any
    operations: Any
    audit: Any
    scheduler: CrawlScheduler
    queue: Any
    events: Any
    briefings: Any
    slack: Any
    workspace: Any
    engine: Any = None
    closeables: tuple[Any, ...] = ()


def _migration_path(settings: Settings) -> Path:
    path = Path(settings.migrations_path)
    if not path.is_absolute() and not path.exists():
        path = Path(__file__).resolve().parents[3] / path
    return path


def _build_runtime(settings: Settings) -> AppRuntime:
    validate_startup_settings(settings)
    backend = settings.repository_backend.lower()
    queue_backend = settings.queue_backend.lower()
    engine = None
    if backend in {"postgres", "postgresql", "sqlalchemy"}:
        configured = session_factory(settings)
        if configured is None:
            raise RuntimeError(
                "DATABASE_URL is required when REPOSITORY_BACKEND=postgres is selected."
            )
        engine, sessions = configured
        monitoring = PostgresMonitoringRepository(sessions)
        crawl = PostgresCrawlRepository(sessions)
        operations_repository = PostgresOperationsRepository(sessions)
        audit = PostgresAuditRepository(sessions)
        events = PostgresEventRepository(sessions)
        briefings = PostgresBriefingRepository(sessions)
        from app.db.repositories import PostgresWorkspaceRepository

        workspace = PostgresWorkspaceRepository(sessions)
    elif backend == "memory":
        monitoring = MonitoringRepository()
        crawl = CrawlRepository()
        operations_repository = None
        audit = MemoryAuditRepository()
        events = MemoryEventRepository()
        briefings = MemoryBriefingRepository()

        class MemoryWorkspaceRepository:
            async def get_role(self, user_id: str, workspace_id: str) -> WorkspaceRole | None:
                return WorkspaceRole.OWNER  # For dev/memory

        workspace = MemoryWorkspaceRepository()
    else:
        raise ValueError("REPOSITORY_BACKEND must be memory or postgres.")

    queue = create_crawl_queue(queue_backend, settings.redis_url)
    closeables: list[Any] = []
    classifier_backend = settings.classifier_backend.lower()
    if classifier_backend == "gemini":
        classifier = GeminiLLMProvider(
            secret_value(settings.gemini_api_key),
            model_name=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
        )
        closeables.append(classifier)
    else:
        classifier = FakeLLMProvider()
    slack_backend = settings.slack_backend.lower()
    slack_enabled = settings.slack_enabled or slack_backend == "slack"
    if slack_enabled:
        slack = SlackApiProvider(
            secret_value(settings.slack_bot_token),
            api_url=str(settings.slack_api_url),
        )
        closeables.append(slack)
    elif settings.app_env.lower() in {"development", "dev", "test", "testing"}:
        slack = FakeSlackProvider()
    else:
        slack = DisabledSlackProvider()
    crawler_policy = CrawlPolicy(
        allow_private_networks=(
            settings.app_env.lower() in {"development", "dev", "test", "testing"}
            or settings.allow_private_crawl_destinations
        )
    )
    service = CrawlService(
        monitoring,
        crawl,
        HttpCrawler(policy=crawler_policy),
        playwright_crawler=PlaywrightCrawler(),
        event_repository=events,
        classifier=classifier,
        classifier_model=(settings.gemini_model if classifier_backend == "gemini" else "fake-v1"),
    )
    operations = CrawlOperations(service, crawl, operations_repository, queue)
    return AppRuntime(
        monitoring=monitoring,
        crawl=crawl,
        operations=operations,
        audit=audit,
        scheduler=CrawlScheduler(operations),
        queue=queue,
        events=events,
        briefings=briefings,
        slack=slack,
        workspace=workspace,
        engine=engine,
        closeables=tuple(closeables),
    )


async def _record_audit(
    repository: Any,
    principal: AuthPrincipal,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    record = build_audit_record(principal, action, entity_type, entity_id, metadata)
    try:
        await maybe_await(repository.save(record))
    except ValueError:
        # Development headers are intentionally free-form; UUID foreign keys are
        # enforced only for durable audit records.
        if not isinstance(repository, PostgresAuditRepository):
            raise


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "source_id": event.source_id,
        "before_snapshot_id": event.before_snapshot_id,
        "after_snapshot_id": event.after_snapshot_id,
        "event_key": event.event_key,
        "category": event.category,
        "title": event.title,
        "observed_facts": event.observed_facts,
        "impact": event.impact,
        "confidence": event.confidence,
        "status": event.status,
        "observed_at": event.observed_at,
        "model_name": event.model_name,
        "schema_version": event.schema_version,
        "version": event.version,
        "user_corrected": event.user_corrected,
    }


def _briefing_payload(briefing: Briefing) -> dict[str, Any]:
    return {
        "briefing_id": briefing.id,
        "period_start": briefing.period_start,
        "period_end": briefing.period_end,
        "status": briefing.status,
        "summary": briefing.summary,
        "events": [
            {
                "event_id": item.event_id,
                "rank": item.rank,
                "title": item.title,
                "category": item.category,
                "impact": item.impact,
                "observed_facts": item.observed_facts,
                "interpretation": item.interpretation,
                "citations": item.citations,
            }
            for item in briefing.events
        ],
        "blocks": briefing.blocks,
    }


async def check_redis(settings: Settings) -> str:
    if not settings.redis_url:
        return "not_configured"
    try:
        from redis import asyncio as redis_asyncio
        from redis.exceptions import RedisError
    except ImportError:
        return "client_missing"
    client = None
    try:
        client = redis_asyncio.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        await client.ping()
        return "ready"
    except (RedisError, OSError, TimeoutError, ValueError):
        return "unavailable"
    finally:
        if client is not None:
            await client.aclose()


async def check_dependencies(settings: Settings) -> dict[str, str]:
    """Run real dependency checks while keeping failures safe and actionable."""
    return {
        "database": await check_database(settings),
        "redis": await check_redis(settings),
        "environment": settings.app_env,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(
        log_level=app_settings.log_level,
        force_json=app_settings.force_json_logging,
    )
    runtime = _build_runtime(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if app_settings.migrate_on_startup:
            if runtime.engine is None:
                raise RuntimeError("MIGRATE_ON_STARTUP requires REPOSITORY_BACKEND=postgres.")
            await run_migrations(runtime.engine, _migration_path(app_settings))
        schedules = (
            await maybe_await(runtime.operations.repository.list_all_schedules())
            if hasattr(runtime.operations.repository, "list_all_schedules")
            else []
        )
        for schedule in schedules:
            if not schedule.active:
                continue
            runtime.scheduler.register_schedule(
                schedule.id,
                schedule.interval_minutes,
                lambda schedule=schedule: runtime.operations.enqueue(
                    schedule.workspace_id,
                    schedule.source_id,
                    f"scheduled:{schedule.id}:{datetime.now(UTC).isoformat()}",
                ),
            )
        await runtime.scheduler.start()
        # Register daily retention purge when running against PostgreSQL
        if runtime.engine is not None:
            from app.retention import purge_old_snapshots

            _sessions = session_factory(app_settings)
            if _sessions is not None:
                _, _session_maker = _sessions
                runtime.scheduler.register_schedule(
                    "__retention_daily__",
                    60 * 24,  # once a day
                    lambda: purge_old_snapshots(_session_maker),
                )
        try:
            yield
        finally:
            await runtime.scheduler.stop()
            close = getattr(runtime.queue, "close", None)
            if close is not None:
                await maybe_await(close())
            for provider in runtime.closeables:
                close = getattr(provider, "close", None)
                if close is not None:
                    await maybe_await(close())
            await dispose_engine(runtime.engine)

    app = FastAPI(title=app_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.dependency_overrides[get_settings] = lambda: app_settings
    monitoring_repository_parameter = Depends(lambda: runtime.monitoring)
    crawl_operations_parameter = Depends(lambda: runtime.operations)
    crawl_repository_parameter = Depends(lambda: runtime.crawl)
    event_repository_parameter = Depends(lambda: runtime.events)
    briefing_repository_parameter = Depends(lambda: runtime.briefings)
    frontend_origin = str(app_settings.frontend_origin).rstrip("/")
    cors_origins = [frontend_origin]
    if "://localhost" in frontend_origin:
        cors_origins.append(frontend_origin.replace("://localhost", "://127.0.0.1"))
    elif "://127.0.0.1" in frontend_origin:
        cors_origins.append(frontend_origin.replace("://127.0.0.1", "://localhost"))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        RateLimitMiddleware,
        requests_per_minute=app_settings.rate_limit_per_minute,
    )
    app.add_middleware(CorrelationMiddleware)

    @app.get("/health", tags=["operations"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": app_settings.app_name}

    @app.get("/ready", tags=["operations"])
    async def ready() -> JSONResponse:
        dependencies = await check_dependencies(app_settings)
        is_ready = all(
            value not in {"not_configured", "unavailable"}
            for key, value in dependencies.items()
            if key != "environment"
        )
        payload = {"status": "ready" if is_ready else "not_ready", "dependencies": dependencies}
        return JSONResponse(status_code=200 if is_ready else 503, content=payload)

    @app.get("/metrics", tags=["operations"])
    async def metrics(format: str | None = None) -> Response:
        collector = get_metrics_collector()
        if format == "json":
            return JSONResponse(content=collector.to_dict())
        return Response(
            content=collector.to_prometheus(),
            media_type="text/plain; version=0.0.4",
        )

    @app.get(
        "/v1/workspaces/{workspace_id}/access-check",
        tags=["workspaces"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def workspace_access_check(
        workspace_id: str,
        principal: AuthPrincipal = workspace_access_parameter,
    ) -> dict[str, str]:
        return {"workspace_id": workspace_id, "user_id": principal.user_id, "role": principal.role}

    @app.post(
        "/v1/workspaces/{workspace_id}/admin-check",
        tags=["workspaces"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def workspace_admin_check(
        workspace_id: str,
        principal: AuthPrincipal = admin_access_parameter,
    ) -> dict[str, str]:
        return {"workspace_id": workspace_id, "user_id": principal.user_id, "role": principal.role}

    @app.post(
        "/v1/workspaces/{workspace_id}/competitors",
        response_model=CompetitorResponse,
        status_code=201,
        tags=["monitoring"],
        dependencies=[Depends(admin_access_dependency)],
    )
    async def create_competitor(
        workspace_id: str,
        data: CompetitorCreate,
        principal: AuthPrincipal = admin_access_parameter,
        repository: Any = monitoring_repository_parameter,
    ) -> CompetitorResponse:
        competitor = await maybe_await(repository.create_competitor(workspace_id, data))
        await _record_audit(
            runtime.audit, principal, "competitor.created", "competitor", competitor.id
        )
        return competitor_response(competitor)

    @app.get(
        "/v1/workspaces/{workspace_id}/competitors",
        response_model=list[CompetitorResponse],
        tags=["monitoring"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def list_competitors(
        workspace_id: str,
        repository: Any = monitoring_repository_parameter,
    ) -> list[CompetitorResponse]:
        items = await maybe_await(repository.list_competitors(workspace_id))
        return [competitor_response(item) for item in items]

    @app.post(
        "/v1/workspaces/{workspace_id}/sources",
        response_model=SourceResponse,
        status_code=201,
        tags=["monitoring"],
        dependencies=[Depends(admin_access_dependency)],
    )
    async def create_source(
        workspace_id: str,
        data: SourceCreate,
        principal: AuthPrincipal = admin_access_parameter,
        repository: Any = monitoring_repository_parameter,
    ) -> SourceResponse:
        source = await maybe_await(repository.create_source(workspace_id, data))
        await _record_audit(runtime.audit, principal, "source.created", "source", source.id)
        return source_response(source)

    @app.get(
        "/v1/workspaces/{workspace_id}/sources",
        response_model=list[SourceResponse],
        tags=["monitoring"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def list_sources(
        workspace_id: str,
        repository: Any = monitoring_repository_parameter,
    ) -> list[SourceResponse]:
        items = await maybe_await(repository.list_sources(workspace_id))
        return [source_response(item) for item in items]

    @app.patch(
        "/v1/workspaces/{workspace_id}/sources/{source_id}",
        response_model=SourceResponse,
        tags=["monitoring"],
        dependencies=[Depends(admin_access_dependency)],
    )
    async def update_source(
        workspace_id: str,
        source_id: str,
        data: SourceUpdate,
        principal: AuthPrincipal = admin_access_parameter,
        repository: Any = monitoring_repository_parameter,
    ) -> SourceResponse:
        source = await maybe_await(repository.update_source(workspace_id, source_id, data))
        await _record_audit(runtime.audit, principal, "source.updated", "source", source.id)
        return source_response(source)

    @app.post(
        "/v1/workspaces/{workspace_id}/sources/{source_id}/crawl",
        tags=["crawling"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def crawl_source(
        workspace_id: str,
        source_id: str,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        principal: AuthPrincipal = workspace_access_parameter,
        operations: Any = crawl_operations_parameter,
    ) -> dict[str, str | bool | None]:
        run = await operations.enqueue(workspace_id, source_id, idempotency_key)
        await _record_audit(runtime.audit, principal, "crawl.queued", "crawl_run", run.id)
        return {
            "run_id": run.id,
            "status": run.status,
            "error_code": run.error_code,
            "retryable": run.retryable,
        }

    @app.get(
        "/v1/workspaces/{workspace_id}/runs",
        tags=["crawling"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def list_runs(
        workspace_id: str,
        repository: Any = crawl_repository_parameter,
    ) -> list[dict[str, str | int | bool | None]]:
        runs = await maybe_await(repository.list_runs(workspace_id))
        return [
            {
                "run_id": run.id,
                "source_id": run.source_id,
                "status": run.status,
                "attempt": run.attempt,
                "duration_ms": run.duration_ms,
                "byte_count": run.byte_count,
                "error_code": run.error_code,
                "retryable": run.retryable,
            }
            for run in runs
        ]

    @app.get(
        "/v1/workspaces/{workspace_id}/runs/{run_id}",
        tags=["crawling"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def get_run(
        workspace_id: str,
        run_id: str,
        repository: Any = crawl_repository_parameter,
    ) -> dict[str, str | int | bool | None]:
        run = await maybe_await(repository.get_run(run_id))
        if run is None or run.workspace_id != workspace_id:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": {
                        "code": "RUN_NOT_FOUND",
                        "message": "Crawl run was not found.",
                    }
                },
            )
        return {
            "run_id": run.id,
            "source_id": run.source_id,
            "status": run.status,
            "attempt": run.attempt,
            "duration_ms": run.duration_ms,
            "byte_count": run.byte_count,
            "error_code": run.error_code,
            "retryable": run.retryable,
        }

    @app.get(
        "/v1/workspaces/{workspace_id}/events",
        tags=["events"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def list_events(
        workspace_id: str,
        category: EventCategory | None = None,
        impact: ImpactLevel | None = None,
        status: EventStatus | None = None,
        limit: int = 50,
        offset: int = 0,
        repository: Any = event_repository_parameter,
    ) -> list[dict[str, Any]]:
        events = await maybe_await(repository.list_events(workspace_id))
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        filtered = [
            event
            for event in events
            if (category is None or event.category is category)
            and (impact is None or event.impact is impact)
            and (status is None or event.status is status)
        ]
        return [_event_payload(event) for event in filtered[offset : offset + limit]]

    @app.get(
        "/v1/workspaces/{workspace_id}/events/{event_id}",
        response_model=None,
        tags=["events"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def get_event_detail(
        workspace_id: str,
        event_id: str,
        repository: Any = event_repository_parameter,
        crawl_repository: Any = crawl_repository_parameter,
    ) -> dict[str, Any] | JSONResponse:
        event = await maybe_await(repository.get_event(workspace_id, event_id))
        if event is None:
            return JSONResponse(
                status_code=404,
                content={"detail": {"code": "EVENT_NOT_FOUND", "message": "Event was not found."}},
            )
        evidence = await maybe_await(repository.list_evidence(event_id))
        snapshots: dict[str, Any] = {}
        for snapshot_id in {event.before_snapshot_id, event.after_snapshot_id} - {None}:
            snapshot = await maybe_await(crawl_repository.get_snapshot(snapshot_id, workspace_id))
            if snapshot is not None:
                snapshots[snapshot_id] = {
                    "snapshot_id": snapshot.id,
                    "fetched_url": snapshot.fetched_url,
                    "fetched_at": snapshot.fetched_at,
                    "content_hash": snapshot.content_hash,
                    "content": snapshot.content,
                    "normalized_sections": snapshot.normalized_sections,
                }
        notes = (
            await maybe_await(repository.list_notes(event_id))
            if hasattr(repository, "list_notes")
            else []
        )
        return {
            **_event_payload(event),
            "evidence": [
                {
                    "evidence_id": item.id,
                    "snapshot_id": item.snapshot_id,
                    "locator": item.locator,
                    "quoted_text": item.quoted_text,
                    "marker": item.marker,
                    "source_url": item.source_url,
                }
                for item in evidence
            ],
            "snapshots": snapshots,
            "notes": [
                {
                    "note_id": item.id,
                    "author_user_id": item.author_user_id,
                    "body": item.body,
                    "created_at": item.created_at,
                }
                for item in notes
            ],
        }

    @app.post(
        "/v1/workspaces/{workspace_id}/events/{event_id}/review",
        response_model=None,
        tags=["events"],
        dependencies=[Depends(review_access_dependency)],
    )
    async def review_event_route(
        workspace_id: str,
        event_id: str,
        data: ReviewRequest,
        principal: AuthPrincipal = review_access_parameter,
        repository: Any = event_repository_parameter,
    ) -> dict[str, Any] | JSONResponse:
        expected_version = data.expected_version
        if expected_version is None:
            expected_version = data.version
        if expected_version is None:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "VERSION_REQUIRED",
                        "message": "expected_version is required for review actions.",
                    }
                },
            )
        try:
            event = await review_event(
                repository,
                workspace_id=workspace_id,
                event_id=event_id,
                action=data.action,
                expected_version=expected_version,
                actor_user_id=principal.user_id,
                category=data.category,
                title=data.title,
                impact=data.impact,
                confidence=data.confidence,
                note=data.note,
            )
        except KeyError:
            return JSONResponse(
                status_code=404,
                content={"detail": {"code": "EVENT_NOT_FOUND", "message": "Event was not found."}},
            )
        except EventVersionConflict:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "EVENT_VERSION_CONFLICT",
                        "message": "The event changed since it was opened; reload and try again.",
                    }
                },
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": "INVALID_REVIEW", "message": str(exc)}},
            )
        await _record_audit(
            runtime.audit,
            principal,
            f"event.{data.action}",
            "change_event",
            event.id,
            {"version": event.version, "note": bool(data.note)},
        )
        return _event_payload(event)

    @app.get(
        "/v1/workspaces/{workspace_id}/events/{event_id}/audit",
        tags=["events"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def event_audit(
        workspace_id: str,
        event_id: str,
    ) -> list[dict[str, Any]]:
        records = (
            await maybe_await(runtime.audit.list_for_entity(workspace_id, event_id))
            if hasattr(runtime.audit, "list_for_entity")
            else []
        )
        return [
            {
                "action": item.action,
                "actor_user_id": item.actor_user_id,
                "entity_type": item.entity_type,
                "metadata": item.metadata_json,
            }
            for item in records
        ]

    @app.post(
        "/v1/workspaces/{workspace_id}/briefings/preview",
        tags=["briefings"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def preview_briefing(
        workspace_id: str,
        data: BriefingPreviewRequest | None = None,
        week_start: date | None = None,
        week_end: date | None = None,
        repository: Any = event_repository_parameter,
        briefing_repository: Any = briefing_repository_parameter,
    ) -> dict[str, Any]:
        default_start, default_end = weekly_period()
        period_start = week_start or (data.period_start if data else None) or default_start
        period_end = (
            week_end or (data.period_end if data else None) or (period_start + timedelta(days=6))
        )
        if period_end < period_start:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "INVALID_BRIEFING_PERIOD",
                        "message": "period_end must be on or after period_start.",
                    }
                },
            )
        events = await maybe_await(repository.list_events(workspace_id))
        evidence = {
            event.id: await maybe_await(repository.list_evidence(event.id)) for event in events
        }
        briefing = generate_briefing(
            workspace_id,
            events,
            evidence,
            period_start,
            period_end,
            data.dashboard_url if data else "http://localhost:5173",
        )
        existing = await maybe_await(
            briefing_repository.find_briefing(workspace_id, period_start, period_end)
        )
        if existing is not None:
            briefing.id = existing.id
        await maybe_await(briefing_repository.save_briefing(briefing))
        return _briefing_payload(briefing)

    @app.post(
        "/v1/workspaces/{workspace_id}/slack/connect",
        tags=["briefings"],
        dependencies=[Depends(admin_access_dependency)],
    )
    async def connect_slack(
        workspace_id: str,
        data: SlackConnectRequest,
        principal: AuthPrincipal = admin_access_parameter,
        briefing_repository: Any = briefing_repository_parameter,
    ) -> dict[str, Any]:
        integration = SlackIntegration(workspace_id, data.team_id, data.token_reference)
        await maybe_await(briefing_repository.save_integration(integration))
        await _record_audit(
            runtime.audit, principal, "slack.connected", "slack_integration", workspace_id
        )
        return {"workspace_id": workspace_id, "team_id": data.team_id, "active": True}

    @app.post(
        "/v1/workspaces/{workspace_id}/slack/destinations",
        tags=["briefings"],
        dependencies=[Depends(admin_access_dependency)],
    )
    async def configure_slack_destination(
        workspace_id: str,
        data: SlackDestinationRequest,
        principal: AuthPrincipal = admin_access_parameter,
        briefing_repository: Any = briefing_repository_parameter,
    ) -> dict[str, Any]:
        destination = SlackDestination(
            id=str(uuid5(NAMESPACE_URL, f"signalforge:{workspace_id}:{data.channel_id}")),
            workspace_id=workspace_id,
            channel_id=data.channel_id,
            channel_name=data.channel_name,
            enabled=data.enabled,
        )
        await maybe_await(briefing_repository.save_destination(destination))
        await _record_audit(
            runtime.audit,
            principal,
            "slack.destination_configured",
            "slack_destination",
            destination.id,
        )
        return {
            "destination_id": destination.id,
            "channel_id": destination.channel_id,
            "channel_name": destination.channel_name,
            "enabled": destination.enabled,
        }

    @app.get(
        "/v1/workspaces/{workspace_id}/briefings/{briefing_id}/delivery",
        tags=["briefings"],
        dependencies=[Depends(workspace_access_dependency)],
    )
    async def briefing_delivery_status(
        workspace_id: str,
        briefing_id: str,
        briefing_repository: Any = briefing_repository_parameter,
    ) -> list[dict[str, Any]]:
        deliveries = await maybe_await(
            briefing_repository.list_deliveries(workspace_id, briefing_id)
        )
        return [
            {
                "delivery_id": item.id,
                "status": item.status,
                "channel_id": item.channel_id,
                "provider_message_id": item.provider_message_id,
                "thread_ts": item.thread_ts,
                "failure_reason": item.failure_reason,
                "idempotency_key": item.idempotency_key,
                "sent_at": item.sent_at,
            }
            for item in deliveries
        ]

    @app.post(
        "/v1/workspaces/{workspace_id}/briefings/{briefing_id}/publish-to-slack",
        response_model=None,
        tags=["briefings"],
        dependencies=[Depends(review_access_dependency)],
    )
    async def publish_briefing_route(
        workspace_id: str,
        briefing_id: str,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        principal: AuthPrincipal = review_access_parameter,
        briefing_repository: Any = briefing_repository_parameter,
    ) -> dict[str, Any] | JSONResponse:
        briefing = await maybe_await(briefing_repository.get_briefing(workspace_id, briefing_id))
        integration = await maybe_await(briefing_repository.get_integration(workspace_id))
        destinations = await maybe_await(briefing_repository.list_destinations(workspace_id))
        destination = next((item for item in destinations if item.enabled), None)
        if briefing is None:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": {"code": "BRIEFING_NOT_FOUND", "message": "Briefing was not found."}
                },
            )
        if integration is None or not integration.active:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "SLACK_NOT_CONNECTED",
                        "message": "Connect a Slack workspace before publishing.",
                    }
                },
            )
        if destination is None:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": {
                        "code": "SLACK_DESTINATION_NOT_CONFIGURED",
                        "message": "Configure a Slack destination first.",
                    }
                },
            )
        key = idempotency_key or f"briefing:{briefing.id}:{destination.channel_id}"
        delivery = await publish_briefing(
            briefing_repository, runtime.slack, briefing, destination, key
        )
        briefing.status = "published" if delivery.status == "sent" else "delivery_failed"
        await maybe_await(briefing_repository.save_briefing(briefing))
        await _record_audit(
            runtime.audit,
            principal,
            "briefing.published",
            "briefing",
            briefing.id,
            {"delivery_status": delivery.status},
        )
        return {
            "delivery_id": delivery.id,
            "status": delivery.status,
            "channel_id": delivery.channel_id,
            "provider_message_id": delivery.provider_message_id,
            "thread_ts": delivery.thread_ts,
            "failure_reason": delivery.failure_reason,
            "idempotency_key": delivery.idempotency_key,
        }

    @app.post(
        "/v1/workspaces/{workspace_id}/sources/{source_id}/schedule",
        tags=["crawling"],
        dependencies=[Depends(admin_access_dependency)],
    )
    async def schedule_source(
        workspace_id: str,
        source_id: str,
        interval_minutes: int = Query(ge=15, le=10080),
        principal: AuthPrincipal = admin_access_parameter,
        operations: Any = crawl_operations_parameter,
    ) -> dict[str, str | int | bool | None]:
        schedule = await operations.create_schedule_async(workspace_id, source_id, interval_minutes)
        runtime.scheduler.register_schedule(
            schedule.id,
            interval_minutes,
            lambda: operations.enqueue(
                workspace_id,
                source_id,
                f"scheduled:{schedule.id}:{datetime.now(UTC).isoformat()}",
            ),
        )
        await _record_audit(
            runtime.audit, principal, "schedule.created", "crawl_schedule", schedule.id
        )
        return {
            "schedule_id": schedule.id,
            "source_id": schedule.source_id,
            "interval_minutes": schedule.interval_minutes,
            "active": schedule.active,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        }

    return app


app = create_app()
