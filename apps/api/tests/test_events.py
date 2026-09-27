from datetime import UTC, datetime

import pytest
from app.domains.crawling import SourceSnapshot
from app.domains.events import (
    ChangeEvent,
    EventCategory,
    EventStatus,
    EvidenceSpan,
    FakeLLMProvider,
    ImpactLevel,
    MemoryEventRepository,
    classify_event,
    create_candidate_events,
)


def _snapshot(identifier: str, text: str, section_key: str = "0:pricing") -> SourceSnapshot:
    return SourceSnapshot(
        id=identifier,
        workspace_id="workspace-a",
        source_id="source-a",
        run_id=f"run-{identifier}",
        fetched_url="https://acme.example/pricing",
        content_type="text/html",
        content=text,
        content_hash=identifier,
        normalized_sections=[
            {
                "key": section_key,
                "heading": "Pricing",
                "text": text,
                "content_hash": identifier,
            }
        ],
        byte_count=len(text),
        fetched_at=datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_changed_snapshot_creates_one_deduplicated_candidate_with_evidence() -> None:
    repository = MemoryEventRepository()
    before = _snapshot("hash-before", "$49")
    after = _snapshot("hash-after", "$59")

    first = await create_candidate_events(repository, before, after)
    second = await create_candidate_events(repository, before, after)

    assert len(first) == 1
    assert second == []
    assert len(repository.events) == 1
    assert {item.marker for item in repository.evidence.values()} == {"before", "after"}


@pytest.mark.anyio
async def test_malformed_classification_routes_event_to_review() -> None:
    repository = MemoryEventRepository()
    after = _snapshot("hash-after", "$59")
    event = ChangeEvent(
        id="event-a",
        workspace_id="workspace-a",
        source_id="source-a",
        before_snapshot_id=None,
        after_snapshot_id=after.id,
        event_key="event-key",
        category=EventCategory.COMPANY_NEWS,
        title="Changed",
        observed_facts=["$59"],
        impact=ImpactLevel.MEDIUM,
        confidence=0,
        status=EventStatus.CANDIDATE,
        observed_at=after.fetched_at,
    )
    repository.save_event(event)
    repository.save_evidence(
        EvidenceSpan(
            id="evidence-a",
            event_id=event.id,
            snapshot_id=after.id,
            locator="0:pricing",
            quoted_text="$59",
            marker="after",
            source_url=after.fetched_url,
        )
    )

    classified = await classify_event(
        repository,
        event,
        None,
        after,
        FakeLLMProvider({"not": "a classification"}),
        "fake-v1",
    )

    assert classified.status is EventStatus.NEEDS_REVIEW


@pytest.mark.anyio
async def test_valid_classification_records_schema_model_and_usage() -> None:
    repository = MemoryEventRepository()
    after = _snapshot("hash-after", "$59")
    events = await create_candidate_events(repository, None, after)

    classified = await classify_event(
        repository, events[0], None, after, FakeLLMProvider(), "fake-v1"
    )

    assert classified.category is EventCategory.COMPANY_NEWS
    assert classified.schema_version == "1.0"
    assert classified.model_name == "fake-v1"
    assert classified.usage_metadata == {"provider": "fake-v1"}
