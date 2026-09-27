ALTER TABLE source_snapshots
    ADD COLUMN IF NOT EXISTS normalized_sections JSONB NOT NULL DEFAULT '[]'::jsonb;
