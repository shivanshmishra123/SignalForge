CREATE TABLE change_events (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    before_snapshot_id UUID REFERENCES source_snapshots(id) ON DELETE SET NULL,
    after_snapshot_id UUID NOT NULL REFERENCES source_snapshots(id),
    event_key VARCHAR(64) NOT NULL,
    category VARCHAR(32) NOT NULL,
    title VARCHAR(255) NOT NULL,
    observed_facts JSONB NOT NULL,
    impact VARCHAR(16) NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    status VARCHAR(24) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    model_name VARCHAR(100),
    schema_version VARCHAR(20),
    classified_at TIMESTAMPTZ,
    usage_metadata JSONB,
    UNIQUE (workspace_id, event_key)
);

CREATE TABLE evidence_spans (
    id UUID PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
    snapshot_id UUID NOT NULL REFERENCES source_snapshots(id) ON DELETE CASCADE,
    locator VARCHAR(255) NOT NULL,
    quoted_text TEXT NOT NULL,
    marker VARCHAR(16) NOT NULL,
    source_url TEXT NOT NULL
);

CREATE INDEX ix_change_events_workspace_observed_at
    ON change_events(workspace_id, observed_at DESC);
