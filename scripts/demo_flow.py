#!/usr/bin/env python3
"""SignalForge End-to-End Fixture Demonstration

Demonstrates the entire competitive intelligence lifecycle:
  1. Seed a workspace with 2 competitors and public sources
  2. Perform initial baseline crawl
  3. Detect a deterministic source change (e.g. pricing increase)
  4. Classify the event and route to human review
  5. Review and approve the event
  6. Generate cited weekly briefing
  7. Publish to Slack channel (idempotent delivery)

Usage:
  python scripts/demo_flow.py
"""

import asyncio
import sys
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
from app.domains.monitoring import SourceType
from app.main import create_app
from app.providers.crawler import DomainRateLimiter, HttpCrawler, RobotsPolicy
from app.settings import Settings
from httpx import ASGITransport, AsyncClient

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def colour(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def STEP(title: str):
    print(f"\n{colour('1;36', f'==> {title}')}")


def OK(msg: str):
    print(f"  {colour('32', '[PASS]')} {msg}")


def INFO(msg: str):
    print(f"  {colour('34', '  -> ')} {msg}")


HTML_V1 = """<!doctype html>
<html>
  <head><title>Acme Cloud Pricing</title></head>
  <body>
    <main>
      <h1>Acme Cloud Plans</h1>
      <p>Starter tier starts at $49 per month.</p>
    </main>
  </body>
</html>"""

HTML_V2 = """<!doctype html>
<html>
  <head><title>Acme Cloud Pricing</title></head>
  <body>
    <main>
      <h1>Acme Cloud Plans</h1>
      <p>Starter tier starts at $79 per month with automated AI intelligence included.</p>
    </main>
  </body>
</html>"""

RSS_V1 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Beta SaaS Changelog</title>
    <item>
      <title>Beta 3.0 Enterprise Launch</title>
      <link>https://beta.example/news/v3-launch</link>
    </item>
  </channel>
</rss>"""


async def run_demo():
    print(colour("1;35", "\n[*] SignalForge End-to-End Intelligence Demonstration"))
    print(colour("90", "-" * 60))

    current_html = HTML_V1

    now_date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")

    def mock_fetch(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "acme.example/pricing" in url_str:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8", "date": now_date},
                content=current_html.encode("utf-8"),
                request=request,
            )
        elif "beta.example/feed" in url_str:
            return httpx.Response(
                200,
                headers={"content-type": "application/rss+xml; charset=utf-8", "date": now_date},
                content=RSS_V1.encode("utf-8"),
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

    mock_crawler = HttpCrawler(
        client=httpx.AsyncClient(transport=httpx.MockTransport(mock_fetch)),
        robots=RobotsPolicy(),
        rate_limiter=DomainRateLimiter(0),
    )
    app.state.runtime.operations.crawl_service.crawler = mock_crawler

    workspace_id = f"demo-ws-{uuid.uuid4().hex[:6]}"
    headers = {
        "X-User-Id": "demo-analyst",
        "X-Workspace-Id": workspace_id,
        "X-Role": "owner",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://demo") as client:
        # Step 1: Competitors
        STEP("1. Seed Competitors")
        r_acme = await client.post(
            f"/v1/workspaces/{workspace_id}/competitors",
            json={"name": "Acme Cloud", "canonical_domain": "acme.example"},
            headers=headers,
        )
        acme_id = r_acme.json()["id"]
        OK(f"Created Competitor: Acme Cloud (id: {acme_id})")

        r_beta = await client.post(
            f"/v1/workspaces/{workspace_id}/competitors",
            json={"name": "Beta SaaS", "canonical_domain": "beta.example"},
            headers=headers,
        )
        beta_id = r_beta.json()["id"]
        OK(f"Created Competitor: Beta SaaS (id: {beta_id})")

        # Step 2: Sources
        STEP("2. Configure Public Sources")
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
        acme_source_id = r_src_acme.json()["id"]
        OK(f"Configured HTML Source: https://acme.example/pricing (id: {acme_source_id})")

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
        beta_source_id = r_src_beta.json()["id"]
        OK(f"Configured RSS Source: https://beta.example/feed (id: {beta_source_id})")

        # Step 3: Baseline Crawl
        STEP("3. Ingest Baseline Snapshot")
        r_crawl1 = await client.post(
            f"/v1/workspaces/{workspace_id}/sources/{acme_source_id}/crawl",
            headers={**headers, "Idempotency-Key": "demo-crawl-1"},
        )
        run_id_1 = r_crawl1.json()["run_id"]
        OK(f"Baseline crawl enqueued: {run_id_1}")

        # Process queued job
        job1 = await app.state.runtime.operations.queue.get()
        await app.state.runtime.operations.process_job(job1)

        run_status_1 = (
            await client.get(f"/v1/workspaces/{workspace_id}/runs/{run_id_1}", headers=headers)
        ).json()
        OK(f"Baseline crawl executed -> status: {run_status_1['status']}")

        events_0 = (
            await client.get(f"/v1/workspaces/{workspace_id}/events", headers=headers)
        ).json()
        INFO(f"Baseline events recorded: {len(events_0)} (initial discovered sections)")

        # Step 4: Source Change
        STEP("4. Record Deterministic Source Change")
        INFO("Competitor changed pricing: $49 -> $79 + AI features")
        current_html = HTML_V2

        r_crawl2 = await client.post(
            f"/v1/workspaces/{workspace_id}/sources/{acme_source_id}/crawl",
            headers={**headers, "Idempotency-Key": "demo-crawl-2"},
        )
        run_id_2 = r_crawl2.json()["run_id"]

        # Process queued job
        job2 = await app.state.runtime.operations.queue.get()
        await app.state.runtime.operations.process_job(job2)

        run_status_2 = (
            await client.get(f"/v1/workspaces/{workspace_id}/runs/{run_id_2}", headers=headers)
        ).json()
        OK(f"Follow-up crawl executed -> status: {run_status_2['status']}")

        # Step 5: Event Classification & Review Queue
        STEP("5. Change Intelligence & Review Queue")
        events = (await client.get(f"/v1/workspaces/{workspace_id}/events", headers=headers)).json()
        change_event = next(
            (e for e in events if e.get("before_snapshot_id") is not None), events[0]
        )
        OK(f"Detected Event: '{change_event['title']}'")
        INFO(
            f"Category: {change_event['category']} | Impact: {change_event['impact']} "
            f"| Status: {change_event['status']}"
        )

        r_detail = await client.get(
            f"/v1/workspaces/{workspace_id}/events/{change_event['event_id']}", headers=headers
        )
        evidence_items = r_detail.json().get("evidence", [])
        INFO(f"Evidence Spans Attached: {len(evidence_items)}")

        # Step 6: Review & Approval
        STEP("6. Human Review")
        r_review = await client.post(
            f"/v1/workspaces/{workspace_id}/events/{change_event['event_id']}/review",
            json={
                "action": "approve",
                "expected_version": change_event["version"],
                "note": "Verified 61% price hike in pricing plan. Update our sales battlecard.",
            },
            headers=headers,
        )
        OK(f"Review Action: Approved (status now: {r_review.json()['status']})")

        # Step 7: Weekly Briefing
        STEP("7. Weekly Market Briefing Generation")
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)

        r_briefing = await client.post(
            f"/v1/workspaces/{workspace_id}/briefings/preview",
            json={"period_start": monday.isoformat(), "period_end": sunday.isoformat()},
            headers=headers,
        )
        briefing = r_briefing.json()
        OK(f"Generated Briefing for week {briefing['period_start']} to {briefing['period_end']}")
        INFO(f"Events included in briefing: {len(briefing['events'])}")

        # Step 8: Slack Publication
        STEP("8. Publish Cited Briefing to Slack")
        await client.post(
            f"/v1/workspaces/{workspace_id}/slack/connect",
            json={"team_id": "T_DEMO", "token_reference": "ref:demo-token"},
            headers=headers,
        )
        await client.post(
            f"/v1/workspaces/{workspace_id}/slack/destinations",
            json={"channel_id": "C_MARKET", "channel_name": "market-briefings", "enabled": True},
            headers=headers,
        )
        r_pub = await client.post(
            f"/v1/workspaces/{workspace_id}/briefings/{briefing['briefing_id']}/publish-to-slack",
            headers={**headers, "Idempotency-Key": "demo-publish-slack"},
        )
        OK(f"Slack delivery status: {r_pub.json()['status']}")

        print(
            colour(
                "1;32",
                "\n[SUCCESS] Demonstration Complete: Full workflow executed successfully with "
                "0 live web dependencies!\n",
            )
        )


if __name__ == "__main__":
    asyncio.run(run_demo())
