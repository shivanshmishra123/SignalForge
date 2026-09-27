from datetime import UTC, date, datetime, timedelta

import pytest
from app.domains.briefings import (
    FakeSlackProvider,
    MemoryBriefingRepository,
    SlackDestination,
    generate_briefing,
    publish_briefing,
)
from app.domains.events import (
    ChangeEvent,
    EventCategory,
    EventStatus,
    EventVersionConflict,
    EvidenceSpan,
    ImpactLevel,
    MemoryEventRepository,
    classify_event,
    review_event,
)


def _event(identifier: str, observed_at: datetime, status=EventStatus.NEEDS_REVIEW) -> ChangeEvent:
    return ChangeEvent(
        id=identifier,
        workspace_id="workspace-a",
        source_id="source-a",
        before_snapshot_id=None,
        after_snapshot_id="snapshot-a",
        event_key=identifier,
        category=EventCategory.PRICING,
        title=f"Change {identifier}",
        observed_facts=[f"Fact {identifier}"],
        impact=ImpactLevel.HIGH,
        confidence=0.4,
        status=status,
        observed_at=observed_at,
    )


@pytest.mark.anyio
async def test_review_uses_optimistic_version_and_preserves_correction() -> None:
    repository = MemoryEventRepository()
    event = _event("event-a", datetime.now(UTC))
    repository.save_event(event)
    repository.save_evidence(
        EvidenceSpan(
            "evidence-a",
            event.id,
            "snapshot-a",
            "body",
            "Fact event-a",
            "after",
            "https://example.test",
        )
    )
    approved = await review_event(
        repository,
        workspace_id="workspace-a",
        event_id=event.id,
        action="approve",
        expected_version=0,
        actor_user_id="reviewer",
    )
    assert approved.status is EventStatus.APPROVED
    assert approved.version == 1
    with pytest.raises(EventVersionConflict):
        await review_event(
            repository,
            workspace_id="workspace-a",
            event_id=event.id,
            action="reject",
            expected_version=0,
            actor_user_id="other",
        )
    await classify_event(
        repository,
        event,
        None,
        # only the early correction guard is relevant for this regression test
        type("Snapshot", (), {"normalized_sections": [], "content": "", "content_hash": "x"})(),
        type("Provider", (), {"classify": lambda *_: {}})(),
        "worker",
    )
    assert repository.events[event.id].status is EventStatus.APPROVED


def test_briefing_filters_and_orders_approved_events_with_citations() -> None:
    start = date(2026, 9, 7)
    events = [
        _event("b", datetime(2026, 9, 8, tzinfo=UTC), EventStatus.APPROVED),
        _event("a", datetime(2026, 9, 8, tzinfo=UTC), EventStatus.APPROVED),
        _event("outside", datetime(2026, 9, 1, tzinfo=UTC), EventStatus.APPROVED),
        _event("pending", datetime(2026, 9, 8, tzinfo=UTC)),
    ]
    evidence = {
        item.id: [
            EvidenceSpan(
                id=f"e-{item.id}",
                event_id=item.id,
                snapshot_id="snapshot-a",
                locator="body",
                quoted_text="evidence",
                marker="after",
                source_url=f"https://example.test/{item.id}",
            )
        ]
        for item in events
    }
    briefing = generate_briefing("workspace-a", events, evidence, start, start + timedelta(days=6))
    assert [item.event_id for item in briefing.events] == ["a", "b"]
    assert briefing.events[0].citations == ["https://example.test/a"]


@pytest.mark.anyio
async def test_slack_delivery_retry_is_idempotent() -> None:
    repository = MemoryBriefingRepository()
    provider = FakeSlackProvider(fail_first=True)
    destination = SlackDestination("destination", "workspace-a", "channel")
    briefing = generate_briefing("workspace-a", [], {}, date(2026, 9, 7), date(2026, 9, 13))
    first = await publish_briefing(repository, provider, briefing, destination, "briefing-key")
    assert first.status == "failed"
    second = await publish_briefing(repository, provider, briefing, destination, "briefing-key")
    third = await publish_briefing(repository, provider, briefing, destination, "briefing-key")
    assert second.status == "sent"
    assert third.id == second.id
    assert provider.attempts == 2
