from dataclasses import asdict
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.models import WorkspaceRole
from app.db.audit import AuditRecord
from app.db.briefing_models import (
    Briefing as BriefingModel,
)
from app.db.briefing_models import (
    Delivery as DeliveryModel,
)
from app.db.briefing_models import (
    SlackDestination as SlackDestinationModel,
)
from app.db.briefing_models import (
    SlackIntegration as SlackIntegrationModel,
)
from app.db.crawling_models import CrawlRun as CrawlRunModel
from app.db.crawling_models import CrawlSchedule as CrawlScheduleModel
from app.db.crawling_models import SourceSnapshot as SourceSnapshotModel
from app.db.event_models import ChangeEvent as ChangeEventModel
from app.db.event_models import EventNote as EventNoteModel
from app.db.event_models import EvidenceSpan as EvidenceSpanModel
from app.db.models import AuditLog
from app.db.monitoring_models import Competitor as CompetitorModel
from app.db.monitoring_models import Source as SourceModel
from app.domains.briefings import (
    Briefing,
    BriefingEvent,
    Delivery,
    SlackDestination,
    SlackIntegration,
)
from app.domains.crawling import CrawlRun, CrawlRunStatus, SourceSnapshot
from app.domains.events import (
    ChangeEvent,
    EventCategory,
    EventNote,
    EventStatus,
    EventVersionConflict,
    EvidenceSpan,
    ImpactLevel,
)
from app.domains.monitoring import (
    Competitor,
    CompetitorCreate,
    Source,
    SourceCreate,
    SourceStatus,
    SourceType,
    SourceUpdate,
    normalize_url,
)
from app.domains.operations import CrawlJob, CrawlSchedule


def _workspace_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"signalforge:{value}")


async def _ensure_workspace(session: AsyncSession, wid: UUID, name: str | None = None) -> None:
    from app.db.models import Workspace

    existing = await session.get(Workspace, wid)
    if existing is None:
        session.add(Workspace(id=wid, name=name or f"Workspace {wid}"))
        await session.flush()


def _competitor(item: CompetitorModel) -> Competitor:
    return Competitor(
        id=str(item.id),
        workspace_id=str(item.workspace_id),
        name=item.name,
        canonical_domain=item.canonical_domain,
        description=item.description,
        active=item.active,
    )


def _source(item: SourceModel) -> Source:
    return Source(
        id=str(item.id),
        workspace_id=str(item.workspace_id),
        competitor_id=str(item.competitor_id),
        source_type=SourceType(item.source_type),
        url=item.url,
        feed_url=item.feed_url,
        normalized_url=item.normalized_url,
        crawl_interval_minutes=item.crawl_interval_minutes,
        parser_key=item.parser_key,
        status=SourceStatus(item.status),
        last_success_at=item.last_success_at,
        last_failure_code=item.last_failure_code,
    )


class PostgresMonitoringRepository:
    """Async PostgreSQL implementation of the monitoring repository boundary."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create_competitor(self, workspace_id: str, data: CompetitorCreate) -> Competitor:
        wid = _workspace_id(workspace_id)
        async with self.sessions() as session:
            await _ensure_workspace(session, wid, workspace_id)
            item = CompetitorModel(
                workspace_id=wid,
                name=data.name,
                canonical_domain=data.canonical_domain.lower().strip(),
                description=data.description,
            )
            session.add(item)
            await session.commit()
            await session.refresh(item)
            return _competitor(item)

    async def list_competitors(self, workspace_id: str) -> list[Competitor]:
        async with self.sessions() as session:
            result = await session.scalars(
                select(CompetitorModel).where(
                    CompetitorModel.workspace_id == _workspace_id(workspace_id)
                )
            )
            return [_competitor(item) for item in result]

    async def get_competitor(self, workspace_id: str, competitor_id: str) -> Competitor:
        async with self.sessions() as session:
            item = await session.scalar(
                select(CompetitorModel).where(
                    CompetitorModel.id == UUID(competitor_id),
                    CompetitorModel.workspace_id == _workspace_id(workspace_id),
                )
            )
            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "COMPETITOR_NOT_FOUND", "message": "Competitor was not found."},
                )
            return _competitor(item)

    async def create_source(self, workspace_id: str, data: SourceCreate) -> Source:
        workspace = _workspace_id(workspace_id)
        normalized = normalize_url(str(data.url))
        async with self.sessions() as session:
            competitor = await session.scalar(
                select(CompetitorModel).where(
                    CompetitorModel.id == UUID(data.competitor_id),
                    CompetitorModel.workspace_id == workspace,
                )
            )
            if competitor is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "COMPETITOR_NOT_FOUND", "message": "Competitor was not found."},
                )
            item = SourceModel(
                workspace_id=workspace,
                competitor_id=competitor.id,
                source_type=data.source_type.value,
                url=normalized,
                feed_url=normalize_url(str(data.feed_url)) if data.feed_url else None,
                normalized_url=normalized,
                crawl_interval_minutes=data.crawl_interval_minutes,
                parser_key=data.parser_key,
            )
            session.add(item)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "DUPLICATE_SOURCE",
                        "message": (
                            "A source with this normalized URL already exists in the workspace."
                        ),
                    },
                ) from exc
            await session.refresh(item)
            return _source(item)

    async def list_sources(self, workspace_id: str) -> list[Source]:
        async with self.sessions() as session:
            result = await session.scalars(
                select(SourceModel).where(SourceModel.workspace_id == _workspace_id(workspace_id))
            )
            return [_source(item) for item in result]

    async def get_source(self, workspace_id: str, source_id: str) -> Source:
        async with self.sessions() as session:
            item = await session.scalar(
                select(SourceModel).where(
                    SourceModel.id == UUID(source_id),
                    SourceModel.workspace_id == _workspace_id(workspace_id),
                )
            )
            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "SOURCE_NOT_FOUND", "message": "Source was not found."},
                )
            return _source(item)

    async def update_source(self, workspace_id: str, source_id: str, data: SourceUpdate) -> Source:
        workspace = _workspace_id(workspace_id)
        async with self.sessions() as session:
            item = await session.scalar(
                select(SourceModel).where(
                    SourceModel.id == UUID(source_id), SourceModel.workspace_id == workspace
                )
            )
            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "SOURCE_NOT_FOUND", "message": "Source was not found."},
                )
            for key, value in data.model_dump(exclude_unset=True).items():
                setattr(item, key, value.value if isinstance(value, SourceStatus) else value)
            await session.commit()
            await session.refresh(item)
            return _source(item)

    async def save_source(self, source: Source) -> Source:
        async with self.sessions() as session:
            item = await session.scalar(
                select(SourceModel).where(
                    SourceModel.id == UUID(source.id),
                    SourceModel.workspace_id == _workspace_id(source.workspace_id),
                )
            )
            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "SOURCE_NOT_FOUND", "message": "Source was not found."},
                )
            item.status = source.status.value
            item.last_success_at = source.last_success_at
            item.last_failure_code = source.last_failure_code
            await session.commit()
            await session.refresh(item)
            return _source(item)


class PostgresCrawlRepository:
    """Async PostgreSQL implementation for idempotent runs and snapshots."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def find_run(
        self, source_id: str, idempotency_key: str, workspace_id: str | None = None
    ) -> CrawlRun | None:
        async with self.sessions() as session:
            filters = [
                CrawlRunModel.source_id == UUID(source_id),
                CrawlRunModel.idempotency_key == idempotency_key,
            ]
            if workspace_id is not None:
                filters.append(CrawlRunModel.workspace_id == UUID(workspace_id))
            item = await session.scalar(select(CrawlRunModel).where(*filters))
            return _run(item) if item else None

    async def save_run(self, run: CrawlRun) -> CrawlRun:
        async with self.sessions() as session:
            item = await session.scalar(
                select(CrawlRunModel).where(
                    CrawlRunModel.id == UUID(run.id),
                    CrawlRunModel.workspace_id == _workspace_id(run.workspace_id),
                    CrawlRunModel.source_id == UUID(run.source_id),
                )
            )
            if item is None:
                item = CrawlRunModel(
                    id=UUID(run.id),
                    workspace_id=UUID(run.workspace_id),
                    source_id=UUID(run.source_id),
                    idempotency_key=run.idempotency_key,
                    status=run.status.value,
                    started_at=run.started_at,
                )
                session.add(item)
            else:
                _copy_run(item, run)
            await session.commit()
            return run

    async def save_snapshot(self, snapshot: SourceSnapshot) -> SourceSnapshot:
        async with self.sessions() as session:
            existing = await session.scalar(
                select(SourceSnapshotModel).where(
                    SourceSnapshotModel.source_id == UUID(snapshot.source_id),
                    SourceSnapshotModel.content_hash == snapshot.content_hash,
                )
            )
            if existing is not None:
                return _snapshot(existing)
            session.add(
                SourceSnapshotModel(
                    id=UUID(snapshot.id),
                    workspace_id=UUID(snapshot.workspace_id),
                    source_id=UUID(snapshot.source_id),
                    run_id=UUID(snapshot.run_id),
                    fetched_url=snapshot.fetched_url,
                    content_type=snapshot.content_type,
                    content=snapshot.content,
                    content_hash=snapshot.content_hash,
                    normalized_sections=snapshot.normalized_sections,
                    byte_count=snapshot.byte_count,
                    fetched_at=snapshot.fetched_at,
                )
            )
            await session.commit()
            return snapshot

    async def get_run(self, run_id: str, workspace_id: str | None = None) -> CrawlRun | None:
        async with self.sessions() as session:
            filters = [CrawlRunModel.id == UUID(run_id)]
            if workspace_id is not None:
                filters.append(CrawlRunModel.workspace_id == _workspace_id(workspace_id))
            item = await session.scalar(select(CrawlRunModel).where(*filters))
            return _run(item) if item else None

    async def list_runs(self, workspace_id: str) -> list[CrawlRun]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(CrawlRunModel).where(
                    CrawlRunModel.workspace_id == _workspace_id(workspace_id)
                )
            )
            return [_run(item) for item in rows]

    async def latest_snapshot(self, source_id: str) -> SourceSnapshot | None:
        async with self.sessions() as session:
            item = await session.scalar(
                select(SourceSnapshotModel)
                .where(SourceSnapshotModel.source_id == UUID(source_id))
                .order_by(SourceSnapshotModel.fetched_at.desc())
            )
            return _snapshot(item) if item else None

    async def get_snapshot(
        self, snapshot_id: str, workspace_id: str | None = None
    ) -> SourceSnapshot | None:
        async with self.sessions() as session:
            filters = [SourceSnapshotModel.id == UUID(snapshot_id)]
            if workspace_id is not None:
                filters.append(SourceSnapshotModel.workspace_id == UUID(workspace_id))
            item = await session.scalar(select(SourceSnapshotModel).where(*filters))
            return _snapshot(item) if item else None


def _run(item: CrawlRunModel) -> CrawlRun:
    return CrawlRun(
        id=str(item.id),
        workspace_id=str(item.workspace_id),
        source_id=str(item.source_id),
        idempotency_key=item.idempotency_key,
        status=CrawlRunStatus(item.status),
        started_at=item.started_at,
        finished_at=item.finished_at,
        error_code=item.error_code,
        retryable=item.retryable,
        attempt=item.attempt,
        duration_ms=item.duration_ms,
        byte_count=item.byte_count,
    )


def _copy_run(item: CrawlRunModel, run: CrawlRun) -> None:
    item.status = run.status.value
    item.finished_at = run.finished_at
    item.error_code = run.error_code
    item.retryable = run.retryable
    item.attempt = run.attempt
    item.duration_ms = run.duration_ms
    item.byte_count = run.byte_count


def _snapshot(item: SourceSnapshotModel) -> SourceSnapshot:
    return SourceSnapshot(
        id=str(item.id),
        workspace_id=str(item.workspace_id),
        source_id=str(item.source_id),
        run_id=str(item.run_id),
        fetched_url=item.fetched_url,
        content_type=item.content_type,
        content=item.content,
        content_hash=item.content_hash,
        normalized_sections=item.normalized_sections,
        byte_count=item.byte_count,
        fetched_at=item.fetched_at,
    )


class PostgresEventRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_event(self, event: ChangeEvent) -> ChangeEvent:
        async with self.sessions() as session:
            item = await session.scalar(
                select(ChangeEventModel).where(ChangeEventModel.id == UUID(event.id))
            )
            if item is None:
                item = ChangeEventModel(
                    id=UUID(event.id),
                    workspace_id=UUID(event.workspace_id),
                    source_id=UUID(event.source_id),
                    before_snapshot_id=UUID(event.before_snapshot_id)
                    if event.before_snapshot_id
                    else None,
                    after_snapshot_id=UUID(event.after_snapshot_id),
                    event_key=event.event_key,
                    category=event.category.value,
                    title=event.title,
                    observed_facts=event.observed_facts,
                    impact=event.impact.value,
                    confidence=event.confidence,
                    status=event.status.value,
                    observed_at=event.observed_at,
                    version=event.version,
                    user_corrected=event.user_corrected,
                )
                session.add(item)
            else:
                item.category = event.category.value
                item.title = event.title
                item.observed_facts = event.observed_facts
                item.impact = event.impact.value
                item.confidence = event.confidence
                item.status = event.status.value
                item.model_name = event.model_name
                item.schema_version = event.schema_version
                item.classified_at = event.classified_at
                item.usage_metadata = event.usage_metadata
                item.version = event.version
                item.user_corrected = event.user_corrected
            await session.commit()
            return event

    async def save_event_if_version(self, event: ChangeEvent, expected_version: int) -> ChangeEvent:
        async with self.sessions() as session:
            result = await session.execute(
                update(ChangeEventModel)
                .where(
                    ChangeEventModel.id == UUID(event.id),
                    ChangeEventModel.workspace_id == _workspace_id(event.workspace_id),
                    ChangeEventModel.version == expected_version,
                )
                .values(
                    category=event.category.value,
                    title=event.title,
                    observed_facts=event.observed_facts,
                    impact=event.impact.value,
                    confidence=event.confidence,
                    status=event.status.value,
                    model_name=event.model_name,
                    schema_version=event.schema_version,
                    classified_at=event.classified_at,
                    usage_metadata=event.usage_metadata,
                    version=event.version,
                    user_corrected=event.user_corrected,
                )
            )
            if result.rowcount != 1:
                await session.rollback()
                raise EventVersionConflict("The event changed since it was opened.")
            await session.commit()
            return event

    async def save_evidence(self, evidence: EvidenceSpan) -> EvidenceSpan:
        async with self.sessions() as session:
            session.add(
                EvidenceSpanModel(
                    id=UUID(evidence.id),
                    event_id=UUID(evidence.event_id),
                    snapshot_id=UUID(evidence.snapshot_id),
                    locator=evidence.locator,
                    quoted_text=evidence.quoted_text,
                    marker=evidence.marker,
                    source_url=evidence.source_url,
                )
            )
            await session.commit()
            return evidence

    async def find_by_key(self, workspace_id: str, event_key: str) -> ChangeEvent | None:
        async with self.sessions() as session:
            item = await session.scalar(
                select(ChangeEventModel).where(
                    ChangeEventModel.workspace_id == _workspace_id(workspace_id),
                    ChangeEventModel.event_key == event_key,
                )
            )
            return _event(item) if item else None

    async def list_events(self, workspace_id: str) -> list[ChangeEvent]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(ChangeEventModel)
                .where(ChangeEventModel.workspace_id == _workspace_id(workspace_id))
                .order_by(ChangeEventModel.observed_at.desc())
            )
            return [_event(item) for item in rows]

    async def list_evidence(self, event_id: str) -> list[EvidenceSpan]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(EvidenceSpanModel).where(EvidenceSpanModel.event_id == UUID(event_id))
            )
            return [_evidence(item) for item in rows]

    async def get_event(self, workspace_id: str, event_id: str) -> ChangeEvent | None:
        async with self.sessions() as session:
            item = await session.scalar(
                select(ChangeEventModel).where(
                    ChangeEventModel.id == UUID(event_id),
                    ChangeEventModel.workspace_id == _workspace_id(workspace_id),
                )
            )
            return _event(item) if item else None

    async def save_note(self, note: EventNote) -> EventNote:
        async with self.sessions() as session:
            session.add(
                EventNoteModel(
                    id=UUID(note.id),
                    event_id=UUID(note.event_id),
                    workspace_id=UUID(note.workspace_id),
                    author_user_id=note.author_user_id,
                    body=note.body,
                    created_at=note.created_at,
                )
            )
            await session.commit()
            return note

    async def list_notes(self, event_id: str) -> list[EventNote]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(EventNoteModel)
                .where(EventNoteModel.event_id == UUID(event_id))
                .order_by(EventNoteModel.created_at.asc(), EventNoteModel.id.asc())
            )
            return [
                EventNote(
                    id=str(item.id),
                    event_id=str(item.event_id),
                    workspace_id=str(item.workspace_id),
                    author_user_id=item.author_user_id,
                    body=item.body,
                    created_at=item.created_at,
                )
                for item in rows
            ]


def _event(item: ChangeEventModel) -> ChangeEvent:
    return ChangeEvent(
        id=str(item.id),
        workspace_id=str(item.workspace_id),
        source_id=str(item.source_id),
        before_snapshot_id=str(item.before_snapshot_id) if item.before_snapshot_id else None,
        after_snapshot_id=str(item.after_snapshot_id),
        event_key=item.event_key,
        category=EventCategory(item.category),
        title=item.title,
        observed_facts=item.observed_facts,
        impact=ImpactLevel(item.impact),
        confidence=item.confidence,
        status=EventStatus(item.status),
        observed_at=item.observed_at,
        model_name=item.model_name,
        schema_version=item.schema_version,
        classified_at=item.classified_at,
        usage_metadata=item.usage_metadata,
        version=item.version,
        user_corrected=item.user_corrected,
    )


def _evidence(item: EvidenceSpanModel) -> EvidenceSpan:
    return EvidenceSpan(
        id=str(item.id),
        event_id=str(item.event_id),
        snapshot_id=str(item.snapshot_id),
        locator=item.locator,
        quoted_text=item.quoted_text,
        marker=item.marker,
        source_url=item.source_url,
    )


class PostgresBriefingRepository:
    """Durable briefing, Slack configuration, and delivery storage."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_briefing(self, briefing: Briefing) -> Briefing:
        async with self.sessions() as session:
            item = await session.scalar(
                select(BriefingModel).where(BriefingModel.id == UUID(briefing.id))
            )
            values = {
                "workspace_id": UUID(briefing.workspace_id),
                "period_start": briefing.period_start,
                "period_end": briefing.period_end,
                "status": briefing.status,
                "summary": briefing.summary,
                "events": [asdict(item) for item in briefing.events],
                "blocks": briefing.blocks,
                "created_at": briefing.created_at,
            }
            if item is None:
                session.add(BriefingModel(id=UUID(briefing.id), **values))
            else:
                for key, value in values.items():
                    setattr(item, key, value)
            await session.commit()
            return briefing

    async def get_briefing(self, workspace_id: str, briefing_id: str) -> Briefing | None:
        async with self.sessions() as session:
            item = await session.scalar(
                select(BriefingModel).where(
                    BriefingModel.id == UUID(briefing_id),
                    BriefingModel.workspace_id == _workspace_id(workspace_id),
                )
            )
            return _briefing(item) if item else None

    async def find_briefing(self, workspace_id: str, period_start, period_end) -> Briefing | None:
        async with self.sessions() as session:
            item = await session.scalar(
                select(BriefingModel).where(
                    BriefingModel.workspace_id == _workspace_id(workspace_id),
                    BriefingModel.period_start == period_start,
                    BriefingModel.period_end == period_end,
                )
            )
            return _briefing(item) if item else None

    async def save_integration(self, integration: SlackIntegration) -> SlackIntegration:
        async with self.sessions() as session:
            item = await session.get(SlackIntegrationModel, _workspace_id(integration.workspace_id))
            if item is None:
                session.add(
                    SlackIntegrationModel(
                        workspace_id=_workspace_id(integration.workspace_id),
                        team_id=integration.team_id,
                        token_reference=integration.token_reference,
                        active=integration.active,
                    )
                )
            else:
                item.team_id = integration.team_id
                item.token_reference = integration.token_reference
                item.active = integration.active
            await session.commit()
            return integration

    async def get_integration(self, workspace_id: str) -> SlackIntegration | None:
        async with self.sessions() as session:
            item = await session.get(SlackIntegrationModel, _workspace_id(workspace_id))
            return (
                SlackIntegration(
                    str(item.workspace_id), item.team_id, item.token_reference, item.active
                )
                if item
                else None
            )

    async def save_destination(self, destination: SlackDestination) -> SlackDestination:
        async with self.sessions() as session:
            item = await session.scalar(
                select(SlackDestinationModel).where(
                    SlackDestinationModel.id == UUID(destination.id)
                )
            )
            if item is None:
                session.add(
                    SlackDestinationModel(
                        id=UUID(destination.id),
                        workspace_id=_workspace_id(destination.workspace_id),
                        channel_id=destination.channel_id,
                        channel_name=destination.channel_name,
                        enabled=destination.enabled,
                    )
                )
            else:
                item.channel_name = destination.channel_name
                item.enabled = destination.enabled
            await session.commit()
            return destination

    async def list_destinations(self, workspace_id: str) -> list[SlackDestination]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(SlackDestinationModel)
                .where(SlackDestinationModel.workspace_id == _workspace_id(workspace_id))
                .order_by(SlackDestinationModel.channel_name, SlackDestinationModel.channel_id)
            )
            return [
                SlackDestination(
                    str(item.id),
                    str(item.workspace_id),
                    item.channel_id,
                    item.channel_name,
                    item.enabled,
                )
                for item in rows
            ]

    async def save_delivery(self, delivery: Delivery) -> Delivery:
        async with self.sessions() as session:
            item = await session.scalar(
                select(DeliveryModel).where(DeliveryModel.id == UUID(delivery.id))
            )
            if item is None:
                session.add(
                    DeliveryModel(
                        id=UUID(delivery.id),
                        briefing_id=UUID(delivery.briefing_id),
                        workspace_id=_workspace_id(delivery.workspace_id),
                        channel_id=delivery.channel_id,
                        idempotency_key=delivery.idempotency_key,
                        provider_message_id=delivery.provider_message_id,
                        thread_ts=delivery.thread_ts,
                        status=delivery.status,
                        failure_reason=delivery.failure_reason,
                        sent_at=delivery.sent_at,
                    )
                )
            else:
                item.provider_message_id = delivery.provider_message_id
                item.thread_ts = delivery.thread_ts
                item.status = delivery.status
                item.failure_reason = delivery.failure_reason
                item.sent_at = delivery.sent_at
            await session.commit()
            return delivery

    async def find_delivery(self, workspace_id: str, idempotency_key: str) -> Delivery | None:
        async with self.sessions() as session:
            item = await session.scalar(
                select(DeliveryModel).where(
                    DeliveryModel.workspace_id == _workspace_id(workspace_id),
                    DeliveryModel.idempotency_key == idempotency_key,
                )
            )
            return _delivery(item) if item else None

    async def list_deliveries(self, workspace_id: str, briefing_id: str) -> list[Delivery]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(DeliveryModel)
                .where(
                    DeliveryModel.workspace_id == _workspace_id(workspace_id),
                    DeliveryModel.briefing_id == UUID(briefing_id),
                )
                .order_by(DeliveryModel.id.asc())
            )
            return [_delivery(item) for item in rows]


def _briefing(item: BriefingModel) -> Briefing:
    # Event rows are materialized in the blocks for durable previews.  The
    # event detail list is reconstructed only for API display.
    return Briefing(
        id=str(item.id),
        workspace_id=str(item.workspace_id),
        period_start=item.period_start,
        period_end=item.period_end,
        summary=item.summary,
        events=[BriefingEvent(**event) for event in item.events],
        blocks=item.blocks,
        status=item.status,
        created_at=item.created_at,
    )


def _delivery(item: DeliveryModel) -> Delivery:
    return Delivery(
        id=str(item.id),
        briefing_id=str(item.briefing_id),
        workspace_id=str(item.workspace_id),
        channel_id=item.channel_id,
        idempotency_key=item.idempotency_key,
        status=item.status,
        provider_message_id=item.provider_message_id,
        thread_ts=item.thread_ts,
        failure_reason=item.failure_reason,
        sent_at=item.sent_at,
    )


class PostgresOperationsRepository:
    """Durable schedule storage; queue locks remain owned by the queue provider."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        # Queue delivery is external; these values retain the local lock/job
        # bookkeeping needed by the worker while run state remains durable.
        self.jobs: dict[str, CrawlJob] = {}
        self.dead_letters: dict[str, CrawlJob] = {}
        self.locks: set[str] = set()

    async def save_schedule(self, schedule: CrawlSchedule) -> CrawlSchedule:
        async with self.sessions() as session:
            session.add(
                CrawlScheduleModel(
                    id=UUID(schedule.id),
                    workspace_id=UUID(schedule.workspace_id),
                    source_id=UUID(schedule.source_id),
                    interval_minutes=schedule.interval_minutes,
                    active=schedule.active,
                    next_run_at=schedule.next_run_at,
                )
            )
            await session.commit()
            return schedule

    async def list_schedules(self, workspace_id: str) -> list[CrawlSchedule]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(CrawlScheduleModel).where(
                    CrawlScheduleModel.workspace_id == _workspace_id(workspace_id)
                )
            )
            return [
                CrawlSchedule(
                    id=str(item.id),
                    workspace_id=str(item.workspace_id),
                    source_id=str(item.source_id),
                    interval_minutes=item.interval_minutes,
                    active=item.active,
                    next_run_at=item.next_run_at,
                )
                for item in rows
            ]

    async def list_all_schedules(self) -> list[CrawlSchedule]:
        async with self.sessions() as session:
            rows = await session.scalars(select(CrawlScheduleModel))
            return [
                CrawlSchedule(
                    id=str(item.id),
                    workspace_id=str(item.workspace_id),
                    source_id=str(item.source_id),
                    interval_minutes=item.interval_minutes,
                    active=item.active,
                    next_run_at=item.next_run_at,
                )
                for item in rows
            ]

    async def save_job(self, job: CrawlJob) -> CrawlJob:
        self.jobs[job.run_id] = job
        return job


class PostgresAuditRepository:
    """Persist state-changing actions when the configured identity uses UUIDs."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save(self, record: AuditRecord) -> AuditRecord:
        wid = _workspace_id(record.workspace_id)
        actor_id = None
        if record.actor_user_id:
            try:
                actor_id = UUID(record.actor_user_id)
            except ValueError:
                actor_id = uuid5(NAMESPACE_URL, f"signalforge:{record.actor_user_id}")
        async with self.sessions() as session:
            await _ensure_workspace(session, wid)
            if actor_id:
                from app.db.models import User

                existing_user = await session.get(User, actor_id)
                if existing_user is None:
                    session.add(
                        User(
                            id=actor_id,
                            external_subject=record.actor_user_id,
                            display_name=record.actor_user_id,
                        )
                    )
                    await session.flush()
            session.add(
                AuditLog(
                    workspace_id=wid,
                    actor_user_id=actor_id,
                    action=record.action,
                    entity_type=record.entity_type,
                    entity_id=record.entity_id,
                    metadata_json=record.metadata_json,
                )
            )
            await session.commit()
        return record

    async def list_for_entity(self, workspace_id: str, entity_id: str) -> list[AuditRecord]:
        async with self.sessions() as session:
            rows = await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.workspace_id == _workspace_id(workspace_id),
                    AuditLog.entity_id == entity_id,
                )
                .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
            )
            return [
                AuditRecord(
                    workspace_id=str(item.workspace_id),
                    actor_user_id=str(item.actor_user_id) if item.actor_user_id else "",
                    action=item.action,
                    entity_type=item.entity_type,
                    entity_id=item.entity_id,
                    metadata_json=item.metadata_json,
                )
                for item in rows
            ]


class PostgresWorkspaceRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def get_role(self, user_id: str, workspace_id: str) -> WorkspaceRole | None:
        async with self.sessions() as session:
            from app.db.models import Membership

            item = await session.scalar(
                select(Membership).where(
                    Membership.user_id == UUID(user_id),
                    Membership.workspace_id == UUID(workspace_id),
                )
            )
            return WorkspaceRole(item.role) if item else None


# A descriptive alias makes the persistence boundary discoverable to callers that
# use the SQLAlchemy spelling rather than the database vendor spelling.
SqlAlchemyWorkspaceRepository = PostgresWorkspaceRepository
SqlAlchemyMonitoringRepository = PostgresMonitoringRepository
SqlAlchemyCrawlRepository = PostgresCrawlRepository
SqlAlchemyOperationsRepository = PostgresOperationsRepository

__all__ = [
    "PostgresCrawlRepository",
    "PostgresMonitoringRepository",
    "PostgresOperationsRepository",
    "PostgresEventRepository",
    "PostgresBriefingRepository",
    "PostgresAuditRepository",
    "PostgresWorkspaceRepository",
    "SqlAlchemyCrawlRepository",
    "SqlAlchemyMonitoringRepository",
    "SqlAlchemyOperationsRepository",
    "SqlAlchemyWorkspaceRepository",
]
