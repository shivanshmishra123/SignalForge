-- Durable operations hardening: retain a distinct terminal state when retries
-- exhaust after a run made partial progress or the source may recover later.
DO $$
DECLARE
    constraint_name text;
BEGIN
    SELECT conname INTO constraint_name
    FROM pg_constraint
    WHERE conrelid = 'crawl_runs'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%status%';
    IF constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE crawl_runs DROP CONSTRAINT %I', constraint_name);
    END IF;
END $$;

ALTER TABLE crawl_runs
    ADD CONSTRAINT crawl_runs_status_check
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'partial_failed', 'dead_letter'));
