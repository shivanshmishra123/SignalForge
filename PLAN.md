# SignalForge implementation plan

## How to use this plan

Each work unit is intended to be one reviewable change, normally a few hundred lines or less. Do not start a later unit until the earlier unit's verification is green. 

## Phase 1: project foundation

### 1.1 Create the development baseline

Checklist:

- [x] Create the Python API/worker package and React TypeScript app.
- [x] Add Docker Compose for Postgres and Redis.
- [x] Add environment configuration with safe development defaults.
- [x] Add formatting, linting, type-checking, test, and CI commands.
- [x] Add health/readiness endpoints and a minimal app shell.
- [x] Add `AGENTS.md` and keep it synchronized with actual commands.

Definition of verified:

- A new checkout can start the documented services.
- API health and readiness checks distinguish “process alive” from “dependencies ready.”
- CI runs formatting, lint, type checks, and a smoke test.

### 1.2 Establish identity and tenant boundaries

Checklist:

- [x] Add user/workspace/membership models and migrations.
- [x] Implement development authentication seam and production provider interface.
- [x] Add workspace roles and authorization dependencies.
- [x] Add audit-log model and helper for state-changing actions.
- [x] Add tests proving cross-workspace reads and writes are denied.

Definition of verified:

- Every protected endpoint has a workspace context.
- Viewer/admin permissions are enforced by tests.
- Audit records include actor, action, entity, and timestamp.

## Phase 2: source and crawl foundation

### 2.1 Add competitors and source configuration

Checklist:

- [x] Add competitor and source models with normalized URLs.
- [x] Support HTML and RSS source types.
- [x] Validate source policy, allowed schemes, and crawl interval.
- [x] Add CRUD endpoints and dashboard forms.
- [x] Add source-level active/paused and failure status.

Definition of verified:

- Duplicate normalized sources are rejected within a workspace.
- Invalid schemes and unsupported source types return stable errors.
- CRUD flows work through API and UI with permission checks.

### 2.2 Implement deterministic fetching

Checklist:

- [x] Define a crawler adapter interface.
- [x] Implement an httpx/BeautifulSoup adapter with timeouts, redirect limits, size caps, and a descriptive user agent.
- [x] Implement Playwright adapter only for an explicit `javascript_required` parser key.
- [x] Add robots/policy check and per-domain concurrency/rate limiting.
- [x] Define crawl-run/snapshot persistence models, migration, and repository boundary.
- [x] Add retry classification for transient versus permanent failures.

Implementation note: the default fixture runtime uses a deterministic in-memory repository, while explicitly configured deployments use the async PostgreSQL adapters added in Phase 2.3B. The in-memory path remains the test double, not the production persistence target.

Definition of verified:

- Fixture HTML/RSS sources can be fetched without live network access in tests.
- Timeout, oversize, blocked, and parse failures are observable and do not overwrite the last successful snapshot.
- Replaying the same run idempotency key does not duplicate snapshots.

### 2.3A Local operational slice (complete)

Checklist:

- [x] Add APScheduler job registration and persisted schedule configuration boundary.
- [x] Enqueue work rather than crawling inside API requests.
- [x] Add worker lifecycle, job locks, retry/backoff, and dead-letter/error state.
- [x] Add run-status endpoints and UI.
- [x] Add metrics for duration, status, retry count, and bytes fetched.
- [x] Validate schedule source ownership and interval bounds.
- [x] Preserve active sources after transient crawl failures and expose partial failure state after retry exhaustion.

Implementation note: the local runtime uses deterministic in-memory queue/schedule/lock repositories. This is the complete fixture-backed development slice and is the default when infrastructure is not configured.

Definition of verified:

- A queued run executes once under a worker lock.
- A failed transient job can be retried without duplicate snapshots.
- The UI/API exposes queued, running, succeeded, partially failed, and failed states.

### 2.3B Durable operations hardening

Checklist:

- [x] Configure SQLAlchemy's async PostgreSQL engine/session lifecycle.
- [x] Add an idempotent runner for the existing plain SQL migrations.
- [x] Add PostgreSQL-backed monitoring, crawl, and schedule repository adapters.
- [x] Add real database and Redis readiness checks with safe unavailable behavior.
- [x] Add an explicit Redis queue abstraction; retain the in-memory queue for deterministic tests.
- [x] Wire the async PostgreSQL adapters and optional Redis queue into the API/worker process through async-aware use-case boundaries.
- [x] Add Docker-backed PostgreSQL/Redis integration tests in CI.

CI now starts Dockerized PostgreSQL and Redis and runs migration/queue smoke tests. Local runs skip those checks unless `INTEGRATION_TESTS=1` is set.

Implementation note: durable adapters and the Redis queue are production-shaped and fail explicitly when their configured dependency or driver is unavailable. The default API remains deterministic and in-memory for tests/local fixture runs. Select them explicitly with `REPOSITORY_BACKEND=postgres` and `QUEUE_BACKEND=redis`; migrations can run before startup or with `MIGRATE_ON_STARTUP=true`. Development-header identities that are not UUIDs cannot create durable audit rows; their state changes still work.

Definition of verified:

- Migrations are applied once in lexical version order and recorded in `schema_migrations`.
- Readiness reports `ready`, `not_configured`, `driver_missing`, or `unavailable` per dependency.
- Durable repository and queue boundaries are testable without live external sites.

## Phase 3: change intelligence

### 3.1 Normalize and diff content

Checklist:
- [x] Build deterministic HTML-to-text and section normalization.
- [x] Remove volatile elements such as timestamps and tracking parameters where safe.
- [x] Store content hash and normalized section hashes.
- [x] Implement added/removed/modified section diffing.
- [x] Add source-specific parser tests and fixture snapshots.

Definition of verified:

- Formatting-only changes do not create noisy events.
- Real section changes identify before and after content.
- Identical content across runs creates no candidate event.

### 3.2 Create candidate events and deduplication

Checklist:

- [x] Define event taxonomy and impact levels.
- [x] Generate candidate events from changed sections.
- [x] Add stable event keys and database uniqueness constraints.
- [x] Link before/after snapshots and evidence spans.
- [x] Add timeline API with pagination and filters.

Definition of verified:

- One source change produces one candidate event after retries or repeated runs.
- Every candidate has source URL, timestamps, and before/after evidence.
- Timeline filtering and pagination are deterministic.

### 3.3 Add structured classification

Checklist:

- [x] Define versioned Pydantic classification schema.
- [x] Add LLM provider interface and one provider adapter.
- [x] Use structured output and validate category, impact, confidence, facts, and evidence references.
- [x] Record model/schema versions and usage metadata.
- [x] Add fake provider and labeled evaluation fixtures.

Implementation note: the crawl service now classifies newly created candidates through the
injectable provider boundary. Development/test uses `FakeLLMProvider`; production can select the
Gemini REST adapter with `CLASSIFIER_BACKEND=gemini`. Gemini is the only production model
provider; orchestration remains the existing worker/domain pipeline rather than a LangGraph
implementation. Evaluation datasets remain deliberate follow-on work.

Definition of verified:

- Malformed model output is rejected or routed to review, never silently accepted.
- Every accepted fact maps to an evidence span.
- Evaluation reports category accuracy, impact agreement, and evidence completeness.

## Phase 4: human review and briefing

### 4.1 Build the review queue

Checklist:

- [x] Route low-confidence and high-impact events to `needs_review`.
- [x] Add event detail view with source evidence and before/after comparison.
- [x] Add approve, reject, edit classification, and add-note actions.
- [x] Add optimistic version checks and audit entries.
- [x] Ensure scheduled reprocessing cannot overwrite user corrections.

Definition of verified:

- Reviewer can resolve an event from the UI.
- Concurrent edits return a clear conflict instead of losing data.
- Correction and decision history are visible in the audit log.

### 4.2 Generate and publish weekly Slack briefings

Checklist:

- [x] Add weekly period selection and approved-event query.
- [x] Generate a concise, structured briefing with deterministic event ordering.
- [x] Include citations and distinguish observed facts from interpretation.
- [x] Add preview endpoint and dashboard preview.
- [x] Add Slack workspace connection and destination-channel configuration.
- [x] Render a readable Slack Block Kit message with summary, event sections, evidence links, and a dashboard link.
- [x] Add Slack adapter, delivery status, and idempotency key.
- [x] Support Slack thread replies or linked dashboard review for follow-up discussion.

Definition of verified:

- A briefing contains only approved in-period events.
- Every material paragraph links to one or more evidence-backed events.
- Publishing the same briefing twice does not create duplicate Slack posts.
- The business team can open each evidence link without needing email.
- Failed delivery is visible and retryable.

## Phase 5: production readiness and portfolio polish

### 5.1 Security, privacy, and reliability hardening

Checklist:
- [x] Add secret-management documentation and startup validation for production dependencies.
- [x] Add request IDs, OpenTelemetry traces, structured logs, and operational metrics.
- [x] Add retention job for snapshots and logs.
- [x] Review HTML sanitization, SSRF protections, redirect handling, and prompt-injection defenses.
- [x] Run dependency and container security checks.

Implementation note: correlation IDs (`X-Correlation-ID`) are assigned or propagated across all HTTP requests and worker tasks; structured JSON logging emits ISO UTC timestamps; token-bucket rate limiting protects workspace APIs with standard `X-RateLimit-*` headers; operational metrics are collected and exported at `/metrics` (Prometheus exposition & JSON); SSRF protection blocks loopbacks, private networks, cloud metadata endpoints, and non-standard crawl ports unless explicitly allowed; snapshot retention jobs purge stale raw snapshots while preserving evidence references and latest source baselines.

Definition of verified:

- Threat-model findings are covered by tests or documented mitigations.
- Operators can locate a failed run from its request/run ID.
- Retention deletes data according to workspace policy without breaking references.

### 5.2 End-to-end demonstration

Checklist:

- [x] Seed a demo workspace with two competitors and fixture sources.
- [x] Record a deterministic source change.
- [x] Show run history, diff, classification, review, timeline, and briefing.
- [x] Add architecture and local-run documentation.
- [x] Add automated end-to-end test and standalone fixture demonstration script (`scripts/demo_flow.py`).

Definition of verified:

- A reviewer can reproduce the full flow locally from a clean checkout.
- The demo does not depend on live websites, personal credentials, or a paid service.
- The README links to `VISION.md`, `ARCHITECTURE.md`, `AGENTS.md`, and this plan.

## Release gates

Do not call the MVP complete until all gates pass:

- [x] All automated checks pass in CI.
- [x] End-to-end fixture flow passes from crawl to briefing.
- [x] No accepted event lacks evidence.
- [x] Tenant-isolation tests pass.
- [x] Retry/idempotency tests pass for runs, events, and Slack delivery.
- [x] Source-compliance and retention behavior is documented.
- [x] LLM evaluation results and known failure modes are documented.

## Explicit follow-on work

After MVP evidence shows demand, consider:

- Temporal for durable multi-step workflows.
- pgvector for semantic event grouping and search.
- Additional connectors with per-source policy adapters.
- Microsoft Teams delivery, if a later customer requires a second collaboration platform.
- Team-level watchlists and alert thresholds.
- Human-labeled evaluation feedback loops.
- Cost-aware model routing and batch classification.

These are intentionally deferred so the initial project proves reliability and product judgment rather than accumulating integrations.
