from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Date, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class Briefing(Base):
    __tablename__ = "briefings"
    __table_args__ = (UniqueConstraint("workspace_id", "period_start", "period_end"),)

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24))
    summary: Mapped[str] = mapped_column(Text)
    events: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    blocks: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SlackIntegration(Base):
    __tablename__ = "slack_integrations"

    workspace_id: Mapped[UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    team_id: Mapped[str] = mapped_column(String(255))
    token_reference: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(default=True)


class SlackDestination(Base):
    __tablename__ = "slack_destinations"
    __table_args__ = (UniqueConstraint("workspace_id", "channel_id"),)

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(String(255))
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    briefing_id: Mapped[UUID] = mapped_column(ForeignKey("briefings.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    channel_id: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    thread_ts: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
