# SignalForge architecture

## Architectural principles

1. **Evidence before inference.** Every event stores source snapshots and exact evidence spans before an LLM interpretation is accepted.
2. **Deterministic work around probabilistic work.** Fetching, hashing, diffing, deduplication, authorization, and delivery are deterministic; the LLM only classifies already-isolated changes.
3. **Retry safely.** Jobs use stable idempotency keys and explicit run states so a worker crash cannot create duplicate events or Slack posts.
4. **Human review is a product feature.** Confidence and impact thresholds route work to review instead of pretending uncertainty does not exist.
5. **Provider boundaries stay replaceable.** LLM, Slack, browser, and queue providers are adapters behind application interfaces.
6. **Respect public-source constraints.** Source configuration records crawl policy, fetch frequency, and failure state. The system does not bypass controls or collect private data.

## Recommended stack

| Concern | Choice | Reasoning |
|---|---|---|
| API | Python 3.12 + FastAPI + Pydantic v2 | Strong typing, fast iteration, async support, and a good hiring signal for backend/applied-AI roles. |
| Workflow orchestration | LangGraph | Makes crawl, diff, classify, review, and delivery states explicit; supports resumability and human interrupts better than a single opaque prompt chain. |
| Durable job execution | Redis-backed worker initially; Temporal as a scale-up option | Redis + a worker keeps the MVP understandable. Temporal becomes worthwhile when schedules, long retries, and cross-service workflows outgrow a single deployment. |
| Scheduler | APScheduler in the application for MVP | Simple daily/weekly schedules without introducing a second orchestration platform. Persisted schedules and a single execution lock prevent duplicate runs. |
| Crawling | httpx + BeautifulSoup; Playwright only for JS-rendered pages | HTTP parsing is cheaper and more reliable. Browser automation is reserved for sources that genuinely require rendering. Crawlers enforce timeouts, redirect/size limits, robots policy, and domain pacing. |
| Database | PostgreSQL 16 | Relational integrity for tenants, runs, evidence, and review state; JSONB supports source-specific metadata without giving up constraints. |
| Similarity search | PostgreSQL full-text search first; pgvector when semantic retrieval is needed | Avoids an extra datastore for MVP. Add embeddings only for search/grouping use cases proven by evaluation. |
| Frontend | React + TypeScript + Vite | Strong component ecosystem and explicit types for timeline, review, and evidence views. |
| Team delivery | Slack bot/API adapter | Puts the weekly briefing in the business team's shared channel, supports rich Block Kit formatting, links back to evidence, and provides delivery status without maintaining a separate email workflow. |
| Observability | OpenTelemetry + structured JSON logs + Prometheus-compatible metrics | Makes fetch failures, LLM cost, queue latency, and traceable workflow runs visible. |
| Local infrastructure | Docker Compose | Reproducible Postgres, Redis, API, worker, and frontend development environment. |
| CI | GitHub Actions | Formatting, type checks, tests, dependency/security checks, and a deterministic integration-test service. |

## System shape

```text
React dashboard
       |
    FastAPI  ---- PostgreSQL (workspace, sources, snapshots, events, review, audit)
       |  \
       |   \---- Redis (job queue, locks, rate-limit state)
       |
  Worker process
       |
  LangGraph workflow
    |        |          |
  fetch    diff      classify/review
    |        |          |
  httpx/Playwright   LLM provider adapter
       |
  Slack provider adapter ---- weekly briefing to business channel
```

The API never performs a long crawl in a request. It creates a queued run and enqueues work. APScheduler registers interval jobs, the worker claims jobs under an idempotency lock, and retries use bounded exponential backoff before entering a dead-letter state; exhausted transient runs remain visible as `partial_failed`. Workers update progress and metrics while the UI polls run status. The local operational repository is deterministic and in-memory for tests and fixture development. When explicitly selected, async PostgreSQL adapters cover monitoring, crawl runs/snapshots, schedules, and audit records; source status and terminal run changes are persisted by the async-aware domain boundary. Redis queueing is an explicit adapter with datetime/enum-safe serialization and a safe, testable unavailable state. `MIGRATE_ON_STARTUP=true` is an explicit startup hook; deployments may instead run the migration module before starting the API. Docker-backed PostgreSQL/Redis integration tests remain a documented CI limitation.

## Suggested repository structure

```text
signalforge/
├── apps/
│   ├── api/
│   │   ├── app/
│   │   │   ├── api/                 # HTTP routes and dependency injection
│   │   │   ├── auth/                # identity, authorization, tenant context
│   │   │   ├── domains/             # source, run, signal, briefing use cases
│   │   │   ├── workflows/            # LangGraph state and nodes
│   │   │   ├── providers/            # crawler, LLM, Slack, clock adapters
│   │   │   ├── db/                   # SQLAlchemy models, repositories, migrations
│   │   │   ├── settings.py
│   │   │   └── main.py
│   │   └── tests/
│   └── web/
│       ├── src/
│       │   ├── features/              # timeline, review, sources, briefing
│       │   ├── components/
│       │   ├── api/
│       │   └── routes/
│       └── tests/
├── packages/
│   └── contracts/                     # generated/shared API schemas
├── migrations/
├── fixtures/                          # deterministic HTML/RSS test sources
├── infra/                             # Docker and deployment configuration
├── scripts/
├── docker-compose.yml
└── pyproject.toml
```

Keep domain logic independent from FastAPI route functions and provider SDKs. A route should authorize, validate, call a use case, and serialize a response—not implement crawling or classification.

The crawler provider returns a normalized document or a classified error (`retryable` versus permanent). Crawl runs use an `Idempotency-Key`; repeated requests return the original run and never create another snapshot. The current deterministic vertical slice uses an in-memory repository behind the same boundary. `app/db/session.py` provides async SQLAlchemy engine/session configuration, `app/db/migrations.py` runs the plain SQL migrations, and `app/db/repositories.py` provides async PostgreSQL monitoring/crawl/schedule adapters. The API and worker wire those adapters through async-aware route/use-case helpers; tests use fake async repositories and never require live infrastructure.

## Core data model

### Workspace and access

- `workspaces`: `id`, `name`, `created_at`, `retention_days`
- `memberships`: `workspace_id`, `user_id`, `role` (`owner`, `admin`, `analyst`, `viewer`), unique pair
- `audit_logs`: actor, workspace, action, entity reference, metadata, timestamp

Every tenant-owned table includes `workspace_id`. Repository methods require the workspace context; never rely on a caller-provided entity ID alone.

### Monitoring

- `competitors`: workspace, name, canonical domain, description, active state
- `sources`: competitor, type, URL, feed URL, crawl policy, parser key, active state, last success/failure
- `crawl_runs`: workspace, source, idempotency key, status, started/finished times, error code, metrics
- `source_snapshots`: source, run, fetched URL, canonical content, content hash, normalized sections, fetched time, HTTP metadata

Unique constraints:

- `sources(workspace_id, normalized_url)`
- `crawl_runs(source_id, idempotency_key)`
- `source_snapshots(source_id, content_hash)`

### Signals and evidence

- `change_events`: workspace, competitor, source, before snapshot, after snapshot, category, title, observed facts, impact, confidence, status, event key
- `evidence_spans`: event, snapshot, locator, quoted text, before/after marker, source URL
- `event_labels`: optional user corrections and taxonomy metadata

An `event_key` should combine source identity, normalized changed content hash, and category. It prevents repeated daily runs from producing the same signal. Event status can be `candidate`, `needs_review`, `approved`, `rejected`, or `superseded`.

### Briefings

- `briefings`: workspace, period start/end, status, generated time, delivery state, Slack channel/thread metadata
- `briefing_events`: briefing, event, rank, included reason
- `deliveries`: briefing, destination type/channel, provider message ID, thread timestamp, status, sent time, failure reason

Slack configuration belongs to the workspace, not source code:

- `slack_integrations`: workspace, Slack team ID, bot installation/team identity, encrypted token reference, active state
- `slack_destinations`: workspace, channel ID, channel name snapshot, enabled state, weekly-post preference

Store channel IDs rather than relying on mutable channel names. The bot must be invited to the configured channel and should request only the scopes needed to publish and link messages. Never persist a raw bot token in application tables or logs.

## Workflow state

The production classifier is now the Gemini REST adapter behind the domain `LLMProvider`
protocol. The current orchestration is the existing worker/domain pipeline with explicit
idempotent stages; LangGraph remains a recommended future evolution and is not implemented or
pretended here.

The LangGraph state should be typed and serializable:

```text
run_id
source_id
snapshot_id
previous_snapshot_id
changed_sections[]
candidate_event_ids[]
classification_results[]
review_required
errors[]
```

Recommended nodes:

1. `load_source_context`
2. `fetch_source`
3. `normalize_and_hash`
4. `persist_snapshot`
5. `compute_diff`
6. `extract_candidate_events`
7. `classify_with_structured_output`
8. `apply_quality_gates`
9. `persist_events`
10. `route_to_review_or_approve`
11. `record_run_result`

The workflow must be restartable from a persisted run. A failed LLM call should not refetch the source or overwrite a successful snapshot.

## Quality and safety constraints

- Use structured LLM output validated by Pydantic/JSON Schema.
- Require evidence spans for every non-empty claim.
- Reject or route to review classifications below the confidence threshold.
- Cap page size, redirect count, crawl depth, and per-domain concurrency.
- Apply robots and source policy checks before fetching.
- Store secrets only in environment/secret-manager configuration.
- Encrypt transport and restrict tenant queries at the repository/service boundary.
- Sanitize HTML before rendering it in the dashboard.
- Treat crawled text as untrusted input; never let page content override system instructions or trigger tools.
- Redact accidental secrets from logs and do not store unnecessary raw response headers.
- Make retention configurable and delete snapshots/events beyond retention.

## API surface

Representative endpoints:

- `POST /v1/workspaces/{id}/competitors`
- `POST /v1/workspaces/{id}/sources`
- `POST /v1/workspaces/{id}/runs`
- `GET /v1/workspaces/{id}/runs/{run_id}`
- `GET /v1/workspaces/{id}/events`
- `GET /v1/workspaces/{id}/events/{event_id}`
- `POST /v1/workspaces/{id}/events/{event_id}/review`
- `POST /v1/workspaces/{id}/briefings/preview`
- `POST /v1/workspaces/{id}/briefings/{briefing_id}/publish-to-slack`
- `POST /v1/workspaces/{id}/slack/connect`
- `POST /v1/workspaces/{id}/slack/destinations`

Return stable error codes, pagination cursors, and request IDs. Use optimistic concurrency/version checks when a reviewer edits an event.

## Testing and evaluation architecture

- Unit tests for normalization, hashing, diffing, event keys, policy checks, and authorization.
- Contract tests for crawler/LLM/Slack adapters.
- Integration tests using Dockerized Postgres and Redis.
- Workflow tests with fake providers and deterministic HTML fixtures.
- End-to-end tests for “source change → review → Slack briefing.”
- An evaluation set with labeled changes for category, impact, confidence, and evidence completeness.
- Never require live competitor websites for CI.

## Deployment path

Start as one API container, one worker container, Postgres, Redis, and a static frontend. Separate the worker only because crawl and LLM workloads have different resource and scaling profiles. Add managed Postgres/Redis, object storage for large snapshots, and Temporal only after measured operational pressure justifies them.
