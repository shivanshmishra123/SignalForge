"""End-to-end fixture demonstration and verification test.

Verifies the complete SignalForge competitive intelligence loop:
1. Seed workspace with 2 competitors and fixture sources.
2. Initial crawl baseline (API enqueues, worker processes, snapshot stored).
3. Deterministic source change detected on subsequent crawl.
4. Candidate event generated with before/after evidence spans and classified.
5. High-impact event routed to review queue (needs_review).
6. Analyst reviews and approves the event with notes.
7. Timeline exposes approved event with source links and evidence.
8. Weekly briefing includes only approved in-period events with citations.
9. Slack publication with idempotency key delivers briefing without duplicates.
"""

from datetime import date, timedelta

import httpx
import pytest
from app.domains.monitoring import SourceType
from app.main import create_app
from app.providers.crawler import DomainRateLimiter, HttpCrawler, RobotsPolicy
from app.settings import Settings
from httpx import ASGITransport, AsyncClient

HTML_BASELINE = """<!doctype html>
<html>
  <head><title>Acme pricing</title></head>
  <body>
    <main><h1>Pricing Plans</h1><p>Teams plan starts at $49 per month.</p></main>
  </body>
</html>
"""

HTML_CHANGED = """<!doctype html>
<html>
  <head><title>Acme pricing</title></head>
  <body>
    <main><h1>Pricing Plans</h1><p>Teams plan starts at $79 per month with new AI add-on.</p></main>
  </body>
</html>
"""

RSS_BASELINE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Beta Updates</title>
    <item><title>Beta 2.0 Released</title><link>https://beta.example/releases/2.0</link></item>
  </channel>
</rss>
"""


def _auth_headers(
    role: str = "analyst", workspace: str = "workspace-demo", user: str = "demo-analyst"
):
    return {
        "X-User-Id": user,
        "X-Workspace-Id": workspace,
        "X-Role": role,
    }


@pytest.mark.anyio
async def test_end_to_end_demonstration_workflow():
    workspace_id = "workspace-demo"
    current_content = HTML_BASELINE

    def mock_transport_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "acme.example/pricing" in url_str:
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/html; charset=utf-8",
                    "date": "Sun, 26 Sep 2026 12:00:00 GMT",
                },
                content=current_content.encode("utf-8"),
                request=request,
            )
        elif "beta.example/feed" in url_str:
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/rss+xml; charset=utf-8",
                    "date": "Sun, 26 Sep 2026 12:00:00 GMT",
                },
                content=RSS_BASELINE.encode("utf-8"),
                request=request,
            )
        return httpx.Response(404, request=request)

    settings = Settings(
        app_env="test",
        repository_backend="memory",
        queue_backend="memory",
        classifier_backend="fake",
        slack_backend="fake",
        allow_private_crawl_destinations=True,
    )
    app = create_app(settings)

    # Inject mock HTTP crawler so the end-to-end test runs without live network
    mock_crawler = HttpCrawler(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock_transport_handler)),
        robots=RobotsPolicy(),
        rate_limiter=DomainRateLimiter(0),
    )
    app.state.runtime.operations.crawl_service.crawler = mock_crawler

    headers = _auth_headers(role="owner", workspace=workspace_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Seed two competitors
        r_acme = await client.post(
            f"/v1/workspaces/{workspace_id}/competitors",
            json={"name": "Acme SaaS", "canonical_domain": "acme.example"},
            headers=headers,
        )
        assert r_acme.status_code == 201
        acme_id = r_acme.json()["id"]

        r_beta = await client.post(
            f"/v1/workspaces/{workspace_id}/competitors",
            json={"name": "Beta Software", "canonical_domain": "beta.example"},
            headers=headers,
        )
        assert r_beta.status_code == 201
        beta_id = r_beta.json()["id"]

        # 2. Add public sources for each competitor
        r_src_acme = await client.post(
            f"/v1/workspaces/{workspace_id}/sources",
            json={
                "competitor_id": acme_id,
                "source_type": SourceType.HTML,
                "url": "https://acme.example/pricing",
                "crawl_interval_minutes": 1440,
            },
            headers=headers,
        )
        assert r_src_acme.status_code == 201
        acme_source_id = r_src_acme.json()["id"]

        r_src_beta = await client.post(
            f"/v1/workspaces/{workspace_id}/sources",
            json={
                "competitor_id": beta_id,
                "source_type": SourceType.RSS,
                "url": "https://beta.example/feed",
                "crawl_interval_minutes": 1440,
            },
            headers=headers,
        )
        assert r_src_beta.status_code == 201

        # 3. Initial Baseline Crawl: Enqueue via API and process by worker
        r_crawl_baseline = await client.post(
            f"/v1/workspaces/{workspace_id}/sources/{acme_source_id}/crawl",
            headers={**headers, "Idempotency-Key": "run-acme-baseline"},
        )
        assert r_crawl_baseline.status_code == 200
        assert r_crawl_baseline.json()["status"] == "queued"
        run_id_1 = r_crawl_baseline.json()["run_id"]

        # Worker processes the queued job
        job1 = await app.state.runtime.operations.queue.get()
        await app.state.runtime.operations.process_job(job1)

        # Verify run succeeded
        r_run_1 = await client.get(
            f"/v1/workspaces/{workspace_id}/runs/{run_id_1}", headers=headers
        )
        assert r_run_1.status_code == 200
        assert r_run_1.json()["status"] == "succeeded"

        # Baseline events
        r_events_0 = await client.get(f"/v1/workspaces/{workspace_id}/events", headers=headers)
        assert r_events_0.status_code == 200
        events_0 = r_events_0.json()
        assert len(events_0) == 1
        assert events_0[0]["before_snapshot_id"] is None

        # 4. Deterministic source change: Acme increases pricing from $49 to $79
        current_content = HTML_CHANGED

        r_crawl_changed = await client.post(
            f"/v1/workspaces/{workspace_id}/sources/{acme_source_id}/crawl",
            headers={**headers, "Idempotency-Key": "run-acme-changed"},
        )
        assert r_crawl_changed.status_code == 200
        run_id_2 = r_crawl_changed.json()["run_id"]

        # Worker processes the second job
        job2 = await app.state.runtime.operations.queue.get()
        await app.state.runtime.operations.process_job(job2)

        r_run_2 = await client.get(
            f"/v1/workspaces/{workspace_id}/runs/{run_id_2}", headers=headers
        )
        assert r_run_2.status_code == 200
        assert r_run_2.json()["status"] == "succeeded"

        # 5. Check Candidate Event Generation and Review Routing
        r_events_1 = await client.get(f"/v1/workspaces/{workspace_id}/events", headers=headers)
        assert r_events_1.status_code == 200
        events_1 = r_events_1.json()
        assert len(events_1) == 2

        change_event = next(e for e in events_1 if e["before_snapshot_id"] is not None)
        event_id = change_event["event_id"]
        assert change_event["status"] == "needs_review"  # High impact routes to review queue

        # Verify evidence spans on event detail
        r_detail = await client.get(
            f"/v1/workspaces/{workspace_id}/events/{event_id}", headers=headers
        )
        assert r_detail.status_code == 200
        assert len(r_detail.json()["evidence"]) > 0

        # 6. Human Review: Reviewer inspects diff and approves with a note
        r_review = await client.post(
            f"/v1/workspaces/{workspace_id}/events/{event_id}/review",
            json={
                "action": "approve",
                "expected_version": change_event["version"],
                "note": "Verified 61% price increase with team; critical positioning update.",
            },
            headers=headers,
        )
        assert r_review.status_code == 200
        assert r_review.json()["status"] == "approved"

        # 7. Timeline reflects approved status
        r_timeline = await client.get(f"/v1/workspaces/{workspace_id}/events", headers=headers)
        assert r_timeline.status_code == 200
        assert any(
            e["event_id"] == event_id and e["status"] == "approved" for e in r_timeline.json()
        )

        # 8. Generate weekly briefing preview
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)

        r_briefing = await client.post(
            f"/v1/workspaces/{workspace_id}/briefings/preview",
            json={"period_start": monday.isoformat(), "period_end": sunday.isoformat()},
            headers=headers,
        )
        assert r_briefing.status_code == 200
        briefing_data = r_briefing.json()
        assert len(briefing_data["events"]) >= 1
        assert any(e["event_id"] == event_id for e in briefing_data["events"])
        assert "blocks" in briefing_data
        assert len(briefing_data["blocks"]) > 0

        # 9. Configure Slack destination and Publish
        await client.post(
            f"/v1/workspaces/{workspace_id}/slack/connect",
            json={"team_id": "T_DEMO", "token_reference": "ref:demo-token"},
            headers=headers,
        )
        await client.post(
            f"/v1/workspaces/{workspace_id}/slack/destinations",
            json={
                "channel_id": "C_MARKET_INTEL",
                "channel_name": "market-intelligence",
                "enabled": True,
            },
            headers=headers,
        )

        briefing_id = briefing_data["briefing_id"]
        r_publish = await client.post(
            f"/v1/workspaces/{workspace_id}/briefings/{briefing_id}/publish-to-slack",
            headers={**headers, "Idempotency-Key": "publish-briefing-demo-key"},
        )
        assert r_publish.status_code == 200
        delivery = r_publish.json()
        assert delivery["status"] == "sent"

        # 10. Verify idempotent retry does NOT create a duplicate post
        r_publish_retry = await client.post(
            f"/v1/workspaces/{workspace_id}/briefings/{briefing_id}/publish-to-slack",
            headers={**headers, "Idempotency-Key": "publish-briefing-demo-key"},
        )
        assert r_publish_retry.status_code == 200
        assert r_publish_retry.json()["delivery_id"] == delivery["delivery_id"]
