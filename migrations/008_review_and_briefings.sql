ALTER TABLE change_events ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE change_events ADD COLUMN IF NOT EXISTS user_corrected BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS event_notes (
    id UUID PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    author_user_id VARCHAR(255) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS slack_integrations (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    team_id VARCHAR(255) NOT NULL,
    token_reference VARCHAR(255) NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS slack_destinations (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    channel_id VARCHAR(255) NOT NULL,
    channel_name VARCHAR(255),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(workspace_id, channel_id)
);

CREATE TABLE IF NOT EXISTS briefings (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    status VARCHAR(24) NOT NULL,
    summary TEXT NOT NULL,
    events JSONB NOT NULL DEFAULT '[]',
    blocks JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(workspace_id, period_start, period_end)
);
ALTER TABLE briefings ADD COLUMN IF NOT EXISTS events JSONB NOT NULL DEFAULT '[]';

CREATE TABLE IF NOT EXISTS briefing_events (
    briefing_id UUID NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
    event_id UUID NOT NULL REFERENCES change_events(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    PRIMARY KEY(briefing_id, event_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id UUID PRIMARY KEY,
    briefing_id UUID NOT NULL REFERENCES briefings(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    channel_id VARCHAR(255) NOT NULL,
    idempotency_key VARCHAR(255) NOT NULL,
    provider_message_id VARCHAR(255),
    thread_ts VARCHAR(255),
    status VARCHAR(24) NOT NULL,
    failure_reason TEXT,
    sent_at TIMESTAMPTZ,
    UNIQUE(workspace_id, idempotency_key)
);
