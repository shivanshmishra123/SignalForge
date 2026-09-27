from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class ChangeEvent(Base):
    __tablename__ = "change_events"
    __table_args__ = (UniqueConstraint("workspace_id", "event_key"),)

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    before_snapshot_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    after_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("source_snapshots.id"))
    event_key: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255))
    observed_facts: Mapped[list[str]] = mapped_column(JSON)
    impact: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column()
    status: Mapped[str] = mapped_column(String(24))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_metadata: Mapped[dict[str, int | float | str] | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(default=0)
    user_corrected: Mapped[bool] = mapped_column(default=False)


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("change_events.id", ondelete="CASCADE"))
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("source_snapshots.id", ondelete="CASCADE"))
    locator: Mapped[str] = mapped_column(String(255))
    quoted_text: Mapped[str] = mapped_column(Text)
    marker: Mapped[str] = mapped_column(String(16))
    source_url: Mapped[str] = mapped_column(Text)


class EventNote(Base):
    __tablename__ = "event_notes"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("change_events.id", ondelete="CASCADE"))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    author_user_id: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
