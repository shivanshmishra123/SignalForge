CREATE TABLE crawl_runs (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    idempotency_key VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL CHECK (
        status IN ('queued', 'running', 'succeeded', 'failed', 'partial_failed', 'dead_letter')
    ),
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    error_code VARCHAR(100),
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (source_id, idempotency_key)
);

CREATE TABLE source_snapshots (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
    fetched_url TEXT NOT NULL,
    content_type VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    byte_count INTEGER NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_id, content_hash)
);

CREATE INDEX ix_crawl_runs_workspace_id ON crawl_runs(workspace_id);
CREATE INDEX ix_source_snapshots_source_id ON source_snapshots(source_id);
