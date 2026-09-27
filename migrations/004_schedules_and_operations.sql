CREATE TABLE crawl_schedules (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    interval_minutes INTEGER NOT NULL CHECK (interval_minutes BETWEEN 15 AND 10080),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    next_run_at TIMESTAMPTZ
);

CREATE INDEX ix_crawl_schedules_workspace_id ON crawl_schedules(workspace_id);
CREATE INDEX ix_crawl_schedules_source_id ON crawl_schedules(source_id);

ALTER TABLE crawl_runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE crawl_runs ADD COLUMN duration_ms INTEGER;
ALTER TABLE crawl_runs ADD COLUMN byte_count INTEGER NOT NULL DEFAULT 0;
