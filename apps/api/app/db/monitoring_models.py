from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    canonical_domain: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "normalized_url", name="uq_source_workspace_normalized_url"
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    competitor_id: Mapped[UUID] = mapped_column(ForeignKey("competitors.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(Text)
    feed_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_url: Mapped[str] = mapped_column(Text)
    crawl_interval_minutes: Mapped[int] = mapped_column(Integer)
    parser_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
