# SignalForge

SignalForge is an evidence-first competitive intelligence platform. It monitors permitted public sources, detects meaningful changes, routes uncertain signals for review, and publishes a cited weekly briefing to a shared Slack channel.

## Local foundation

Requirements: Python 3.12+, Node.js 20+, npm, and Docker Desktop.

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

For the complete deterministic local application, keep the defaults in `.env`:
`REPOSITORY_BACKEND=memory`, `QUEUE_BACKEND=memory`, and empty database/Redis URLs. Start
the API, a local fixture server, and the frontend in separate PowerShell terminals:

```powershell
# Terminal 1 - API
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="apps/api"
python -m uvicorn app.main:app --app-dir apps/api --reload
```

```powershell
# Terminal 2 - deterministic public-source fixtures
python -m http.server 9000 --directory fixtures
```

```powershell
# Terminal 3 - frontend
Set-Location apps/web
npm install
npm run dev
```

Open `http://localhost:5173`. The API is available at `http://localhost:8000`; verify it with:

```powershell
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:8000/ready
```

Configure a source as `http://localhost:9000/competitor.html` or
`http://localhost:9000/competitor.rss` from the dashboard. The local fixture server is only for
development and keeps the workflow independent of live competitor websites.

### Durable local mode

Use this mode when you want PostgreSQL and Redis persistence instead of the deterministic
in-memory runtime:

```powershell
docker compose up -d
Copy-Item .env.example .env -Force
@"
DATABASE_URL=postgresql://signalforge:signalforge@localhost:5432/signalforge
REDIS_URL=redis://localhost:6379/0
REPOSITORY_BACKEND=postgres
QUEUE_BACKEND=redis
MIGRATE_ON_STARTUP=true
"@ | Add-Content .env
```

Start the API and frontend using the same commands above. Durable mode requires workspace and
user UUIDs in the PostgreSQL identity tables; use memory mode for the header-based demo workspace
(`workspace-a`) unless you seed durable identity records first.

For a clean shutdown:

```powershell
docker compose down
```

The frontend requires Node.js 20+ and npm.

Legacy frontend-only command:

```powershell
Set-Location apps/web
npm install
npm run dev
```

API health is available at `http://localhost:8000/health`. `/ready` performs real database and Redis checks and returns `503` when either dependency is not configured, missing its driver, or unavailable. The default fixture/test runtime remains dependency-free.

Protected workspace endpoints currently use an explicit development authentication seam:

```powershell
curl.exe http://localhost:8000/v1/workspaces/workspace-a/access-check `
  -H "X-User-Id: user-a" `
  -H "X-Workspace-Id: workspace-a" `
  -H "X-Role: analyst"
```

The identity schema is defined in `migrations/001_identity_and_tenant_boundaries.sql`. Production identity providers can replace the development header provider without changing route authorization.

Monitoring configuration is available in the web app and API. Competitor/source schema changes are defined in `migrations/002_competitors_and_sources.sql`; the default fixture slice uses a deterministic repository; async PostgreSQL monitoring/crawl/schedule adapters are documented below for explicitly configured deployments.

Crawling is available with `POST /v1/workspaces/{workspace_id}/sources/{source_id}/crawl` and an `Idempotency-Key` header. Tests use `httpx.MockTransport` and local fixtures, never live competitor websites. Crawl run and snapshot schema is defined in `migrations/003_crawl_runs_and_snapshots.sql`.

Operational scheduling is available with `POST /v1/workspaces/{workspace_id}/sources/{source_id}/schedule?interval_minutes=1440`. The API validates source ownership and accepts intervals from 15 to 10080 minutes. Requests enqueue work and return a queued run; the worker performs the crawl asynchronously. Run history is available at `GET /v1/workspaces/{workspace_id}/runs` and exhausted transient retries are reported as `partial_failed`.

Successful crawls normalize and diff snapshots, create deduplicated evidence-backed events, and
classify them through the injectable provider boundary. The event timeline is available at
`GET /v1/workspaces/{workspace_id}/events` with `category`, `impact`, `status`, `limit`, and
`offset` filters. The default fixture runtime uses the deterministic fake classifier.

Review and briefings are available in the dashboard and API. Events below 70% confidence or with
high impact enter `needs_review`; reviewers can inspect before/after snapshots and evidence,
approve/reject, edit classification, and add notes using optimistic `expected_version` checks.
`POST /v1/workspaces/{workspace_id}/briefings/preview` selects an ISO week and includes only
approved events, with deterministic ordering, observed facts, interpretations, and source citations.
Configure a channel with `/slack/connect` and `/slack/destinations`, then publish with an
`Idempotency-Key`; failed deliveries remain visible and retryable. The fixture runtime uses a fake
Slack provider and never contacts a real workspace.

## Production deployment

Production uses Gemini through the REST API as the only model provider. The checked-in
`.env.production.example` is a template only; put real values in the platform's secret manager.
Provision managed PostgreSQL 16 and Redis 7, create a Gemini API key, and create a Slack app with
the `chat:write` bot scope. Install the app in the workspace, invite it to the destination
channel, and keep the bot token out of source control.

1. Copy the production template into deployment configuration and set `DATABASE_URL`,
   `REDIS_URL`, `FRONTEND_ORIGIN`, `GEMINI_API_KEY`, and `SLACK_BOT_TOKEN` (or set
   `SLACK_BACKEND=fake` and `SLACK_ENABLED=false` if Slack delivery is intentionally deferred).
2. Build and deploy the API image from the repository root:

   ```powershell
   docker build -t signalforge-api -f Dockerfile .
   docker run --rm --env-file .env.production signalforge-api python -m app.db.migrations
   docker run --env-file .env.production -p 8000:8000 signalforge-api
   ```

   The API lifespan starts the scheduler and worker loop, so this single image covers the
   API/worker deployment for the current pipeline. A platform that separates processes can run
   the same image and environment for both roles; both must share PostgreSQL and Redis settings.
3. Build and deploy the static frontend:

   ```powershell
   docker build --build-arg VITE_API_URL=https://api.example.com `
     -t signalforge-web -f apps/web/Dockerfile .
   ```

   Point the frontend's API base URL at the API service, configure TLS and the API
   `FRONTEND_ORIGIN` CORS value, and expose `/health` and `/ready` to the platform health checks.
4. Configure scheduled worker execution and verify a crawl, a classification, and a Slack
   delivery from the dashboard. Rotate database, Redis, Gemini, and Slack secrets through the
   secret manager on a regular schedule and after any suspected exposure.

`CLASSIFIER_BACKEND=gemini` is required in production; fake classification is available only in
development/test. Slack is real only when `SLACK_BACKEND=slack` or `SLACK_ENABLED=true`; a
production process without Slack configured fails delivery explicitly rather than publishing to a
fake workspace. Gemini calls are made by the existing worker/domain pipeline; the repository
does not add or pretend to implement LangGraph orchestration.

The repository provides both development-header authentication for local development and an Auth0 JWT bearer token authenticator (`app/auth0.py`) for production deployments.

## End-to-end demonstration

To run the complete automated end-to-end demonstration without any external cloud or website dependencies:

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="apps/api"
python scripts/demo_flow.py
```

This self-contained workflow executes:
1. **Competitor Seeding**: Creates two competitor profiles in an isolated workspace.
2. **Source Configuration**: Adds public HTML and RSS feed sources.
3. **Baseline Ingestion**: Runs a baseline crawl with deterministic mock responses to establish initial content sections.
4. **Source Change Detection**: Simulates a competitor pricing shift ($49 $\rightarrow$ $79 + AI features) and executes a follow-up crawl.
5. **Change Intelligence**: Normalizes content, diffs sections, attaches evidence spans, and classifies the event via the LLM boundary.
6. **Human Review**: Routes the detected event to the review queue and records an explicit approval with analyst notes.
7. **Weekly Briefing**: Compiles an ISO-week intelligence briefing cited directly to evidence-backed events.
8. **Slack Delivery**: Dispatches the formatted Block Kit payload to a configured destination channel with an idempotency key.

Automated end-to-end regression tests are also included in `apps/api/tests/test_e2e_flow.py`.

## Observability, metrics, and security hardening

- **Operational Metrics**: `GET /metrics` provides real-time counts for HTTP requests, crawler runs, detected events, review actions, and Slack deliveries in both standard Prometheus exposition format and JSON (`Accept: application/json`).
- **Structured Logging & Correlation**: All HTTP requests and scheduled operations assign or propagate an `X-Correlation-ID` header. JSON-structured logs include ISO UTC timestamps, path, method, status, and duration.
- **Rate Limiting**: In-memory token-bucket rate limiting protects workspace API routes with standard `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers.
- **SSRF Protection**: Crawler policies strictly reject private IPv4/IPv6 ranges, loopbacks, link-local addresses, cloud metadata endpoints (e.g. `169.254.169.254`), and non-standard crawl ports (allowing only 80, 443, and 9000 for local dev).
- **Snapshot Retention**: The retention manager purges aged raw snapshots while preserving referenced evidence spans and latest source baseline snapshots.

## Project documents

- [VISION.md](VISION.md): product purpose and definition of done.
- [ARCHITECTURE.md](ARCHITECTURE.md): technology and system decisions.
- [AGENTS.md](AGENTS.md): persistent engineering instructions.
- [PLAN.md](PLAN.md): reviewable implementation units and verification gates.

### Operations durability

`apps/api/app/db/session.py` configures the async PostgreSQL engine/session and `apps/api/app/db/migrations.py` applies the existing plain SQL files idempotently. With `DATABASE_URL` set, from the repository root run `$env:PYTHONPATH="apps/api"; python -m app.db.migrations` (or invoke `run_migrations` from deployment code) before selecting durable repositories. `PostgresMonitoringRepository`, `PostgresCrawlRepository`, and `PostgresOperationsRepository` provide the async persistence boundary.

The default app intentionally keeps deterministic in-memory repositories for tests and fixture development. To enable durable API/worker execution, set `REPOSITORY_BACKEND=postgres`, `DATABASE_URL`, and `QUEUE_BACKEND=redis`/`REDIS_URL`. The API lifespan starts the worker against those adapters, persists run/source status updates, and restores persisted schedules. `MIGRATE_ON_STARTUP=true` applies migrations before the worker starts; otherwise run the migration module as a deployment hook. `RedisCrawlQueue` serializes datetimes/enums and raises a configuration error rather than silently falling back when Redis is selected. CI starts Docker-backed PostgreSQL and Redis and runs migration and queue smoke tests; local runs skip these checks unless `INTEGRATION_TESTS=1` is set.

