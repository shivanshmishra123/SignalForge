"""Snapshot and audit log retention job.

Deletes snapshots older than the configured retention window that are not
referenced by any accepted change event.  Separately purges audit log rows
beyond the audit retention window.

Design notes
------------
- Runs as a one-shot async function so it can be triggered by a scheduler,
  a cron job on Render, or a management CLI command.
- Never deletes a snapshot that is still referenced by an approved event —
  those are permanent evidence.
- Preserves the most recent snapshot per source so we always have a baseline
  for diff detection.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


async def purge_old_snapshots(
    sessions: async_sessionmaker[AsyncSession],
    snapshot_retention_days: int = 90,
    audit_retention_days: int = 365,
) -> dict[str, int]:
    """Delete old snapshots and audit rows, return counts of deleted rows."""
    from sqlalchemy import func

    from app.db.crawling_models import SourceSnapshot as SnapshotModel
    from app.db.event_models import ChangeEvent as ChangeEventModel
    from app.db.models import AuditLog

    now = datetime.now(UTC)
    snapshot_cutoff = now - timedelta(days=snapshot_retention_days)
    audit_cutoff = now - timedelta(days=audit_retention_days)
    deleted = {"snapshots": 0, "audit_rows": 0}

    async with sessions() as session:
        # Find snapshot IDs referenced by any change event (as before/after)
        result = await session.execute(
            select(ChangeEventModel.before_snapshot_id, ChangeEventModel.after_snapshot_id)
        )
        protected: set[str] = set()
        for before_id, after_id in result.all():
            if before_id:
                protected.add(before_id)
            if after_id:
                protected.add(after_id)

        # Also protect the newest snapshot per source
        newest_result = await session.execute(
            select(func.max(SnapshotModel.id)).group_by(SnapshotModel.source_id)
        )
        for (newest_id,) in newest_result.all():
            if newest_id:
                protected.add(str(newest_id))

        # Delete old snapshots that are not protected
        del_result = await session.execute(
            delete(SnapshotModel)
            .where(
                SnapshotModel.fetched_at < snapshot_cutoff,
                SnapshotModel.id.notin_(protected) if protected else True,
            )
            .returning(SnapshotModel.id)
        )
        deleted["snapshots"] = len(del_result.fetchall())

        # Delete old audit log rows
        audit_result = await session.execute(
            delete(AuditLog).where(AuditLog.created_at < audit_cutoff).returning(AuditLog.id)
        )
        deleted["audit_rows"] = len(audit_result.fetchall())

        await session.commit()

    logger.info(
        "Retention purge complete",
        extra={
            "deleted_snapshots": deleted["snapshots"],
            "deleted_audit_rows": deleted["audit_rows"],
            "snapshot_retention_days": snapshot_retention_days,
            "audit_retention_days": audit_retention_days,
        },
    )
    return deleted
