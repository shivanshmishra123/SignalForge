from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from app.domains.async_utils import maybe_await
from app.domains.content import NormalizedDocument, NormalizedSection, SectionChange

if TYPE_CHECKING:
    from app.domains.crawling import SourceSnapshot


class EventCategory(StrEnum):
    PRICING = "pricing"
    PRODUCT_FEATURE = "product_feature"
    POSITIONING = "positioning"
    HIRING = "hiring"
    PARTNERSHIP = "partnership"
    FUNDING = "funding"
    COMPANY_NEWS = "company_news"


class ImpactLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EventStatus(StrEnum):
    CANDIDATE = "candidate"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass
class EvidenceSpan:
    id: str
    event_id: str
    snapshot_id: str
    locator: str
    quoted_text: str
    marker: str
    source_url: str


@dataclass
class EventNote:
    id: str
    event_id: str
    workspace_id: str
    author_user_id: str
    body: str
    created_at: datetime


@dataclass
class ChangeEvent:
    id: str
    workspace_id: str
    source_id: str
    before_snapshot_id: str | None
    after_snapshot_id: str
    event_key: str
    category: EventCategory
    title: str
    observed_facts: list[str]
    impact: ImpactLevel
    confidence: float
    status: EventStatus
    observed_at: datetime
    model_name: str | None = None
    schema_version: str | None = None
    classified_at: datetime | None = None
    usage_metadata: dict[str, int | float | str] | None = None
    # Incremented on every user mutation.  Workers must treat this as a
    # compare-and-swap token rather than replacing reviewer decisions.
    version: int = 0
    user_corrected: bool = False


class EventRepository(Protocol):
    async def save_event(self, event: ChangeEvent) -> ChangeEvent: ...
    async def save_evidence(self, evidence: EvidenceSpan) -> EvidenceSpan: ...
    async def find_by_key(self, workspace_id: str, event_key: str) -> ChangeEvent | None: ...
    async def list_events(self, workspace_id: str) -> list[ChangeEvent]: ...
    async def list_evidence(self, event_id: str) -> list[EvidenceSpan]: ...
    async def get_event(self, workspace_id: str, event_id: str) -> ChangeEvent | None: ...
    async def save_note(self, note: EventNote) -> EventNote: ...
    async def list_notes(self, event_id: str) -> list[EventNote]: ...


class MemoryEventRepository:
    def __init__(self) -> None:
        self.events: dict[str, ChangeEvent] = {}
        self.evidence: dict[str, EvidenceSpan] = {}
        self.notes: dict[str, EventNote] = {}

    def save_event(self, event: ChangeEvent) -> ChangeEvent:
        existing = self.find_by_key(event.workspace_id, event.event_key)
        if existing is not None:
            if existing.id == event.id:
                self.events[event.id] = event
            return existing
        self.events[event.id] = event
        return event

    def save_event_if_version(self, event: ChangeEvent, expected_version: int) -> ChangeEvent:
        current = self.events.get(event.id)
        if current is None or current.version != expected_version:
            raise EventVersionConflict("The event changed since it was opened.")
        self.events[event.id] = event
        return event

    def save_evidence(self, evidence: EvidenceSpan) -> EvidenceSpan:
        self.evidence[evidence.id] = evidence
        return evidence

    def find_by_key(self, workspace_id: str, event_key: str) -> ChangeEvent | None:
        return next(
            (
                event
                for event in self.events.values()
                if event.workspace_id == workspace_id and event.event_key == event_key
            ),
            None,
        )

    def list_events(self, workspace_id: str) -> list[ChangeEvent]:
        return sorted(
            (event for event in self.events.values() if event.workspace_id == workspace_id),
            key=lambda event: event.observed_at,
            reverse=True,
        )

    def list_evidence(self, event_id: str) -> list[EvidenceSpan]:
        return [item for item in self.evidence.values() if item.event_id == event_id]

    def get_event(self, workspace_id: str, event_id: str) -> ChangeEvent | None:
        event = self.events.get(event_id)
        return event if event and event.workspace_id == workspace_id else None

    def save_note(self, note: EventNote) -> EventNote:
        self.notes[note.id] = note
        return note

    def list_notes(self, event_id: str) -> list[EventNote]:
        return sorted(
            (item for item in self.notes.values() if item.event_id == event_id),
            key=lambda item: (item.created_at, item.id),
        )


class EventVersionConflict(ValueError):
    """Raised when a reviewer submits a stale event version."""


async def review_event(
    repository: EventRepository,
    *,
    workspace_id: str,
    event_id: str,
    action: str,
    expected_version: int,
    actor_user_id: str,
    category: EventCategory | None = None,
    title: str | None = None,
    impact: ImpactLevel | None = None,
    confidence: float | None = None,
    note: str | None = None,
) -> ChangeEvent:
    event = await maybe_await(repository.get_event(workspace_id, event_id))
    if event is None:
        raise KeyError("EVENT_NOT_FOUND")
    if event.version != expected_version:
        raise EventVersionConflict("The event changed since it was opened.")
    if action not in {"approve", "reject", "edit_classification", "add_note"}:
        raise ValueError("Unsupported review action.")
    if action == "add_note" and (not note or not note.strip()):
        raise ValueError("A note is required.")
    if action == "approve":
        evidence = await maybe_await(repository.list_evidence(event.id))
        if not evidence:
            raise ValueError("An event needs source evidence before it can be approved.")
    event = deepcopy(event)
    if action == "approve":
        event.status = EventStatus.APPROVED
        event.user_corrected = True
    elif action == "reject":
        event.status = EventStatus.REJECTED
        event.user_corrected = True
    elif action == "edit_classification":
        if category is not None:
            event.category = category
        if title is not None:
            event.title = title
        if impact is not None:
            event.impact = impact
        if confidence is not None:
            event.confidence = confidence
        event.user_corrected = True
        if event.status is EventStatus.CANDIDATE:
            event.status = EventStatus.NEEDS_REVIEW
    event.version += 1
    versioned_save = getattr(repository, "save_event_if_version", None)
    if versioned_save is not None:
        try:
            await maybe_await(versioned_save(event, expected_version))
        except EventVersionConflict:
            raise
    else:
        await maybe_await(repository.save_event(event))
    if note:
        await maybe_await(
            repository.save_note(
                EventNote(
                    id=str(uuid4()),
                    event_id=event.id,
                    workspace_id=workspace_id,
                    author_user_id=actor_user_id,
                    body=note.strip(),
                    created_at=datetime.now(UTC),
                )
            )
        )
    return event


class ClassificationInput(BaseModel):
    schema_version: str = Field(pattern=r"^1\.[0-9]+$")
    category: EventCategory
    title: str = Field(min_length=1, max_length=255)
    observed_facts: list[str] = Field(min_length=1)
    impact: ImpactLevel
    confidence: float = Field(ge=0, le=1)
    evidence_references: list[str] = Field(min_length=1)
    usage_metadata: dict[str, int | float | str] = Field(default_factory=dict)


class ClassificationError(ValueError):
    pass


class LLMProvider(Protocol):
    async def classify(
        self, before: NormalizedDocument | None, after: NormalizedDocument
    ) -> object: ...


class FakeLLMProvider:
    def __init__(self, result: object | None = None, model_name: str = "fake-v1") -> None:
        self.result = result
        self.model_name = model_name

    async def classify(
        self, before: NormalizedDocument | None, after: NormalizedDocument
    ) -> object:
        if self.result is not None:
            return self.result
        section = after.sections[0] if after.sections else None
        return {
            "schema_version": "1.0",
            "category": EventCategory.COMPANY_NEWS,
            "title": section.heading if section else "Source changed",
            "observed_facts": [section.text] if section and section.text else ["Source changed"],
            "impact": ImpactLevel.MEDIUM,
            "confidence": 0.5,
            "evidence_references": [section.key] if section else ["body"],
            "usage_metadata": {"provider": self.model_name},
        }


def event_key(source_id: str, normalized_hash: str, category: EventCategory) -> str:
    return sha256(f"{source_id}:{normalized_hash}:{category.value}".encode()).hexdigest()


def normalized_document(snapshot: SourceSnapshot) -> NormalizedDocument:
    sections = tuple(
        NormalizedSection(
            item["key"], item.get("heading", ""), item.get("text", ""), item["content_hash"]
        )
        for item in snapshot.normalized_sections
    )
    return NormalizedDocument(snapshot.content, snapshot.content_hash, sections)


async def create_candidate_events(
    repository: EventRepository,
    before: SourceSnapshot | None,
    after: SourceSnapshot,
) -> list[ChangeEvent]:
    after_document = normalized_document(after)
    before_document = normalized_document(before) if before else None
    changes = _changes(before_document, after_document)
    created: list[ChangeEvent] = []
    for change in changes:
        section = change.after or change.before
        if section is None:
            continue
        key = event_key(after.source_id, section.content_hash, EventCategory.COMPANY_NEWS)
        existing = await maybe_await(repository.find_by_key(after.workspace_id, key))
        if existing is not None:
            continue
        event = ChangeEvent(
            id=str(uuid4()),
            workspace_id=after.workspace_id,
            source_id=after.source_id,
            before_snapshot_id=before.id if before else None,
            after_snapshot_id=after.id,
            event_key=key,
            category=EventCategory.COMPANY_NEWS,
            title=section.heading or "Source content changed",
            observed_facts=[section.text] if section.text else [change.change_type],
            impact=ImpactLevel.MEDIUM,
            confidence=0,
            status=EventStatus.CANDIDATE,
            observed_at=after.fetched_at,
        )
        saved = await maybe_await(repository.save_event(event))
        evidence = [
            EvidenceSpan(
                id=str(uuid4()),
                event_id=saved.id,
                snapshot_id=after.id,
                locator=section.key,
                quoted_text=section.text or section.heading,
                marker="after",
                source_url=after.fetched_url,
            )
        ]
        if before and change.before is not None:
            evidence.append(
                EvidenceSpan(
                    id=str(uuid4()),
                    event_id=saved.id,
                    snapshot_id=before.id,
                    locator=change.before.key,
                    quoted_text=change.before.text or change.before.heading,
                    marker="before",
                    source_url=before.fetched_url,
                )
            )
        for item in evidence:
            await maybe_await(repository.save_evidence(item))
        created.append(saved)
    return created


async def classify_event(
    repository: EventRepository,
    event: ChangeEvent,
    before: SourceSnapshot | None,
    after: SourceSnapshot,
    provider: LLMProvider,
    model_name: str,
) -> ChangeEvent:
    # A scheduled run can reclassify a candidate, but never undo a decision or
    # an explicit correction made by a reviewer.
    if event.user_corrected or event.status in {
        EventStatus.APPROVED,
        EventStatus.REJECTED,
    }:
        return event
    classification_version = event.version
    event = deepcopy(event)
    evidence = await maybe_await(repository.list_evidence(event.id))
    valid_locators = {item.locator for item in evidence}
    try:
        result = ClassificationInput.model_validate(
            await maybe_await(
                provider.classify(
                    normalized_document(before) if before else None, normalized_document(after)
                )
            )
        )
        if len(result.observed_facts) != len(result.evidence_references) or not set(
            result.evidence_references
        ).issubset(valid_locators):
            raise ClassificationError("Classification referenced evidence that does not exist.")
    except (ValidationError, ClassificationError, TypeError, ValueError, RuntimeError):
        event.status = EventStatus.NEEDS_REVIEW
        event.classified_at = datetime.now(UTC)
        await _save_classification(repository, event, classification_version)
        return event

    event.category = result.category
    event.title = result.title
    event.observed_facts = result.observed_facts
    event.impact = result.impact
    event.confidence = result.confidence
    event.model_name = model_name
    event.schema_version = result.schema_version
    event.classified_at = datetime.now(UTC)
    event.usage_metadata = result.usage_metadata
    event.status = (
        EventStatus.NEEDS_REVIEW
        if result.confidence < 0.7 or result.impact is ImpactLevel.HIGH
        else EventStatus.CANDIDATE
    )
    await _save_classification(repository, event, classification_version)
    return event


async def _save_classification(
    repository: EventRepository, event: ChangeEvent, expected_version: int
) -> None:
    versioned_save = getattr(repository, "save_event_if_version", None)
    if versioned_save is None:
        await maybe_await(repository.save_event(event))
        return
    try:
        await maybe_await(versioned_save(event, expected_version))
    except EventVersionConflict:
        # A reviewer won the race.  Their durable correction remains intact;
        # the scheduled worker simply abandons this stale classification.
        return


def _changes(
    before: NormalizedDocument | None, after: NormalizedDocument
) -> tuple[SectionChange, ...]:
    if before is None:
        return tuple(
            SectionChange(section.key, "added", None, section) for section in after.sections
        )
    from app.domains.content import diff_sections

    return diff_sections(before, after)
