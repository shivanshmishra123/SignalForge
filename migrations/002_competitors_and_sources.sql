CREATE TABLE competitors (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    canonical_domain VARCHAR(255) NOT NULL,
    description TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX ix_competitors_workspace_id ON competitors(workspace_id);

CREATE TABLE sources (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    competitor_id UUID NOT NULL REFERENCES competitors(id) ON DELETE CASCADE,
    source_type VARCHAR(16) NOT NULL CHECK (source_type IN ('html', 'rss')),
    url TEXT NOT NULL,
    feed_url TEXT,
    normalized_url TEXT NOT NULL,
    crawl_interval_minutes INTEGER NOT NULL CHECK (crawl_interval_minutes BETWEEN 15 AND 10080),
    parser_key VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'failed')),
    last_success_at TIMESTAMPTZ,
    last_failure_code VARCHAR(100),
    UNIQUE (workspace_id, normalized_url)
);

CREATE INDEX ix_sources_workspace_id ON sources(workspace_id);
CREATE INDEX ix_sources_competitor_id ON sources(competitor_id);

