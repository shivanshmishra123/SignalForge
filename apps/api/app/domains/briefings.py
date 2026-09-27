"""Deterministic weekly briefing generation and provider boundaries."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from app.domains.async_utils import maybe_await
from app.domains.events import ChangeEvent, EventStatus, EvidenceSpan


@dataclass
class BriefingEvent:
    event_id: str
    rank: int
    title: str
    category: str
    impact: str
    observed_facts: list[str]
    interpretation: str
    citations: list[str]


@dataclass
class Briefing:
    id: str
    workspace_id: str
    period_start: date
    period_end: date
    summary: str
    events: list[BriefingEvent]
    blocks: list[dict[str, object]]
    status: str = "preview"
    created_at: datetime | None = None


@dataclass
class SlackIntegration:
    workspace_id: str
    team_id: str
    token_reference: str
    active: bool = True


@dataclass
class SlackDestination:
    id: str
    workspace_id: str
    channel_id: str
    channel_name: str | None = None
    enabled: bool = True


@dataclass
class Delivery:
    id: str
    briefing_id: str
    workspace_id: str
    channel_id: str
    idempotency_key: str
    status: str = "pending"
    provider_message_id: str | None = None
    thread_ts: str | None = None
    failure_reason: str | None = None
    sent_at: datetime | None = None


class BriefingRepository(Protocol):
    async def save_briefing(self, briefing: Briefing) -> Briefing: ...
    async def get_briefing(self, workspace_id: str, briefing_id: str) -> Briefing | None: ...
    async def find_briefing(
        self, workspace_id: str, period_start: date, period_end: date
    ) -> Briefing | None: ...
    async def save_integration(self, integration: SlackIntegration) -> SlackIntegration: ...
    async def get_integration(self, workspace_id: str) -> SlackIntegration | None: ...
    async def save_destination(self, destination: SlackDestination) -> SlackDestination: ...
    async def list_destinations(self, workspace_id: str) -> list[SlackDestination]: ...
    async def save_delivery(self, delivery: Delivery) -> Delivery: ...
    async def find_delivery(self, workspace_id: str, idempotency_key: str) -> Delivery | None: ...
    async def list_deliveries(self, workspace_id: str, briefing_id: str) -> list[Delivery]: ...


class MemoryBriefingRepository:
    def __init__(self) -> None:
        self.briefings: dict[str, Briefing] = {}
        self.integrations: dict[str, SlackIntegration] = {}
        self.destinations: dict[str, SlackDestination] = {}
        self.deliveries: dict[str, Delivery] = {}

    def save_briefing(self, briefing: Briefing) -> Briefing:
        self.briefings[briefing.id] = briefing
        return briefing

    def get_briefing(self, workspace_id: str, briefing_id: str) -> Briefing | None:
        item = self.briefings.get(briefing_id)
        return item if item and item.workspace_id == workspace_id else None

    def find_briefing(
        self, workspace_id: str, period_start: date, period_end: date
    ) -> Briefing | None:
        return next(
            (
                item
                for item in self.briefings.values()
                if item.workspace_id == workspace_id
                and item.period_start == period_start
                and item.period_end == period_end
            ),
            None,
        )

    def save_integration(self, integration: SlackIntegration) -> SlackIntegration:
        self.integrations[integration.workspace_id] = integration
        return integration

    def get_integration(self, workspace_id: str) -> SlackIntegration | None:
        return self.integrations.get(workspace_id)

    def save_destination(self, destination: SlackDestination) -> SlackDestination:
        self.destinations[destination.id] = destination
        return destination

    def list_destinations(self, workspace_id: str) -> list[SlackDestination]:
        return sorted(
            (item for item in self.destinations.values() if item.workspace_id == workspace_id),
            key=lambda item: (item.channel_name or "", item.channel_id),
        )

    def save_delivery(self, delivery: Delivery) -> Delivery:
        self.deliveries[delivery.id] = delivery
        return delivery

    def find_delivery(self, workspace_id: str, idempotency_key: str) -> Delivery | None:
        return next(
            (
                item
                for item in self.deliveries.values()
                if item.workspace_id == workspace_id and item.idempotency_key == idempotency_key
            ),
            None,
        )

    def list_deliveries(self, workspace_id: str, briefing_id: str) -> list[Delivery]:
        return sorted(
            (
                item
                for item in self.deliveries.values()
                if item.workspace_id == workspace_id and item.briefing_id == briefing_id
            ),
            key=lambda item: item.id,
        )


class SlackProvider(Protocol):
    async def publish(
        self, *, channel_id: str, blocks: list[dict[str, object]], idempotency_key: str
    ) -> tuple[str, str]: ...

    async def reply(
        self, *, channel_id: str, thread_ts: str, text: str, idempotency_key: str
    ) -> tuple[str, str]: ...


class FakeSlackProvider:
    """Deterministic Slack substitute; retries return the original message."""

    def __init__(self, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.messages: dict[str, tuple[str, str, list[dict[str, object]]]] = {}
        self.attempts = 0

    async def publish(
        self, *, channel_id: str, blocks: list[dict[str, object]], idempotency_key: str
    ) -> tuple[str, str]:
        self.attempts += 1
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("Slack provider temporarily unavailable.")
        if idempotency_key in self.messages:
            message_id, thread_ts, _ = self.messages[idempotency_key]
            return message_id, thread_ts
        message_id = f"fake-{sha256(idempotency_key.encode()).hexdigest()[:12]}"
        thread_ts = f"{self.attempts}.000000"
        self.messages[idempotency_key] = (message_id, thread_ts, blocks)
        return message_id, thread_ts

    async def reply(
        self, *, channel_id: str, thread_ts: str, text: str, idempotency_key: str
    ) -> tuple[str, str]:
        return await self.publish(
            channel_id=channel_id,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            idempotency_key=idempotency_key,
        )


def weekly_period(value: date | None = None) -> tuple[date, date]:
    """Return an ISO week (Monday through Sunday) containing ``value``."""
    current = value or datetime.now(UTC).date()
    start = current - timedelta(days=current.weekday())
    return start, start + timedelta(days=6)


def approved_events(
    events: list[ChangeEvent], period_start: date, period_end: date
) -> list[ChangeEvent]:
    return sorted(
        (
            item
            for item in events
            if item.status is EventStatus.APPROVED
            and period_start <= item.observed_at.date() <= period_end
        ),
        key=lambda item: (
            -{"high": 3, "medium": 2, "low": 1}[item.impact.value],
            item.observed_at,
            item.id,
        ),
    )


def render_slack_blocks(briefing: Briefing, dashboard_url: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"SignalForge weekly briefing · {briefing.period_start}",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": briefing.summary}},
        {"type": "divider"},
    ]
    for item in briefing.events:
        citations = " ".join(f"<{url}|evidence>" for url in item.citations)
        review_link = f"<{dashboard_url}/events/{item.event_id}|review in dashboard>"
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{item.rank}. {item.title}* · `{item.impact}`\n"
                        f"*Observed:* {'; '.join(item.observed_facts)}\n"
                        f"*Interpretation:* {item.interpretation}\n{citations} {review_link}"
                    ),
                },
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"<{dashboard_url}/briefings/{briefing.id}|Open SignalForge dashboard>",
                }
            ],
        }
    )
    return blocks


def generate_briefing(
    workspace_id: str,
    events: list[ChangeEvent],
    evidence: dict[str, list[EvidenceSpan]],
    period_start: date,
    period_end: date,
    dashboard_url: str = "http://localhost:5173",
) -> Briefing:
    selected = approved_events(events, period_start, period_end)
    items: list[BriefingEvent] = []
    for rank, event in enumerate(selected, 1):
        citations = sorted({span.source_url for span in evidence.get(event.id, [])})
        interpretation = (
            f"This {event.category.value.replace('_', ' ')} change may warrant follow-up."
        )
        items.append(
            BriefingEvent(
                event_id=event.id,
                rank=rank,
                title=event.title,
                category=event.category.value,
                impact=event.impact.value,
                observed_facts=list(event.observed_facts),
                interpretation=interpretation,
                citations=citations,
            )
        )
    summary = (
        f"{len(items)} approved signal{'s' if len(items) != 1 else ''} observed "
        f"from {period_start} through {period_end}."
    )
    briefing = Briefing(
        id=str(uuid4()),
        workspace_id=workspace_id,
        period_start=period_start,
        period_end=period_end,
        summary=summary,
        events=items,
        blocks=[],
        created_at=datetime.now(UTC),
    )
    briefing.blocks = render_slack_blocks(briefing, dashboard_url)
    return briefing


async def publish_briefing(
    repository: BriefingRepository,
    provider: SlackProvider,
    briefing: Briefing,
    destination: SlackDestination,
    idempotency_key: str,
) -> Delivery:
    existing = await maybe_await(repository.find_delivery(briefing.workspace_id, idempotency_key))
    if existing and existing.status == "sent":
        return existing
    delivery = existing or Delivery(
        id=str(uuid4()),
        briefing_id=briefing.id,
        workspace_id=briefing.workspace_id,
        channel_id=destination.channel_id,
        idempotency_key=idempotency_key,
    )
    try:
        message_id, thread_ts = await maybe_await(
            provider.publish(
                channel_id=destination.channel_id,
                blocks=briefing.blocks,
                idempotency_key=idempotency_key,
            )
        )
    except (RuntimeError, TimeoutError, OSError) as exc:
        delivery.status = "failed"
        delivery.failure_reason = str(exc)
        await maybe_await(repository.save_delivery(delivery))
        return delivery
    delivery.status = "sent"
    delivery.provider_message_id = message_id
    delivery.thread_ts = thread_ts
    delivery.sent_at = datetime.now(UTC)
    await maybe_await(repository.save_delivery(delivery))
    return delivery
