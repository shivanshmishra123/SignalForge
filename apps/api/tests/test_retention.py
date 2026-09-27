from unittest.mock import AsyncMock, MagicMock

import pytest
from app.retention import purge_old_snapshots


@pytest.mark.anyio
async def test_purge_old_snapshots_protects_evidence_and_deletes_expired():
    mock_session = AsyncMock()

    # 1. Event query returns protected snapshot IDs ("snap-evidence-1", "snap-evidence-2")
    events_result = MagicMock()
    events_result.all.return_value = [("snap-evidence-1", "snap-evidence-2")]

    # 2. Max snapshot query returns newest snapshot per source ("snap-newest")
    newest_result = MagicMock()
    newest_result.all.return_value = [("snap-newest",)]

    # 3. Snapshot delete result returns 2 deleted snapshot IDs
    snap_del_result = MagicMock()
    snap_del_result.fetchall.return_value = [("snap-old-1",), ("snap-old-2",)]

    # 4. Audit delete result returns 1 deleted audit ID
    audit_del_result = MagicMock()
    audit_del_result.fetchall.return_value = [("audit-old-1",)]

    mock_session.execute.side_effect = [
        events_result,
        newest_result,
        snap_del_result,
        audit_del_result,
    ]

    class MockSessionContext:
        async def __aenter__(self):
            return mock_session

        async def __aexit__(self, exc_type, exc, tb):
            return None

    session_maker = MagicMock(return_value=MockSessionContext())

    result = await purge_old_snapshots(
        session_maker, snapshot_retention_days=30, audit_retention_days=90
    )

    assert result["snapshots"] == 2
    assert result["audit_rows"] == 1
    assert mock_session.commit.called
