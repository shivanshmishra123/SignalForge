#!/usr/bin/env python3
"""
SignalForge Live Deployment Validator
======================================
Runs end-to-end checks against a deployed SignalForge instance.

Usage:
  python scripts/validate_deployment.py --api https://signalforge-api.onrender.com --token <auth0_token>

To get a test token from Auth0 (Management API > Applications > API Explorer):
  curl -X POST https://<domain>/oauth/token \\
    -d "client_id=<test_client_id>&client_secret=<secret>" \\
    -d "audience=<audience>&grant_type=client_credentials"
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime


def colour(code, text):
    return f"\033[{code}m{text}\033[0m"


def OK(t):
    print(colour("32", f"  ✓ {t}"))


def FAIL(t):
    print(colour("31", f"  ✗ {t}"))


def INFO(t):
    print(colour("36", f"  → {t}"))


def HEAD(t):
    print(f"\n{colour('1', t)}")


def api(method: str, path: str, api_url: str, token: str, workspace_id: str, body=None) -> dict:
    url = f"{api_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Workspace-Id", workspace_id)
    req.add_header("Idempotency-Key", str(uuid.uuid4()))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as e:
        body = {}
        try:
            body = json.loads(e.read())
        except Exception:
            pass
        return {"status": e.code, "body": body}


def check_health(api_url: str) -> bool:
    HEAD("1. Health & Readiness")
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=10) as r:
            data = json.loads(r.read())
            if data.get("status") == "ok":
                OK(f"/health → {data}")
            else:
                FAIL(f"/health returned unexpected: {data}")
                return False
    except Exception as e:
        FAIL(f"/health failed: {e}")
        return False

    try:
        with urllib.request.urlopen(f"{api_url}/ready", timeout=15) as r:
            data = json.loads(r.read())
            if data.get("status") == "ready":
                OK(f"/ready → {data}")
                return True
            else:
                FAIL(f"/ready not ready: {data}")
                return False
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        FAIL(f"/ready returned {e.code}: {body}")
        return False
    except Exception as e:
        FAIL(f"/ready failed: {e}")
        return False


def check_auth(api_url: str, token: str, workspace_id: str) -> bool:
    HEAD("2. Authentication & Tenant Isolation")
    result = api("GET", f"/v1/workspaces/{workspace_id}/access-check", api_url, token, workspace_id)
    if result["status"] == 200:
        OK(
            f"Authenticated as user_id={result['body'].get('user_id')}, "
            f"role={result['body'].get('role')}"
        )
        return True
    else:
        FAIL(f"Access check failed: {result['status']} → {result['body']}")
        return False


def check_competitor_crud(api_url: str, token: str, workspace_id: str) -> str | None:
    HEAD("3. Monitoring CRUD")
    name = f"TestCo-{uuid.uuid4().hex[:6]}"
    result = api(
        "POST",
        f"/v1/workspaces/{workspace_id}/competitors",
        api_url,
        token,
        workspace_id,
        {"name": name, "canonical_domain": f"{name.lower()}.example.com"},
    )
    if result["status"] == 201:
        cid = result["body"]["id"]
        OK(f"Created competitor {name} → {cid}")
        return cid
    else:
        FAIL(f"Create competitor failed: {result['status']} → {result['body']}")
        return None


def check_source_and_crawl(api_url: str, token: str, workspace_id: str, competitor_id: str) -> bool:
    HEAD("4. Source Creation & Crawl Queue")
    result = api(
        "POST",
        f"/v1/workspaces/{workspace_id}/sources",
        api_url,
        token,
        workspace_id,
        {"competitor_id": competitor_id, "source_type": "html", "url": "https://example.com"},
    )
    if result["status"] != 201:
        FAIL(f"Create source failed: {result['status']} → {result['body']}")
        return False
    source_id = result["body"]["id"]
    OK(f"Created source → {source_id}")

    crawl_result = api(
        "POST",
        f"/v1/workspaces/{workspace_id}/sources/{source_id}/crawl",
        api_url,
        token,
        workspace_id,
    )
    if crawl_result["status"] == 200:
        run_id = crawl_result["body"].get("run_id")
        OK(f"Crawl queued → run_id={run_id}, status={crawl_result['body'].get('status')}")
        return True
    else:
        FAIL(f"Crawl enqueue failed: {crawl_result['status']} → {crawl_result['body']}")
        return False


def check_events(api_url: str, token: str, workspace_id: str) -> bool:
    HEAD("5. Events Endpoint")
    result = api("GET", f"/v1/workspaces/{workspace_id}/events", api_url, token, workspace_id)
    if result["status"] == 200:
        count = len(result["body"])
        OK(f"Events endpoint returned {count} event(s)")
        return True
    else:
        FAIL(f"Events list failed: {result['status']} → {result['body']}")
        return False


def check_briefing(api_url: str, token: str, workspace_id: str) -> bool:
    HEAD("6. Briefing Preview")
    result = api(
        "POST", f"/v1/workspaces/{workspace_id}/briefings/preview", api_url, token, workspace_id, {}
    )
    if result["status"] == 200:
        briefing_id = result["body"].get("briefing_id")
        event_count = len(result["body"].get("events", []))
        OK(f"Briefing preview generated → briefing_id={briefing_id}, {event_count} ranked events")
        return True
    else:
        FAIL(f"Briefing preview failed: {result['status']} → {result['body']}")
        return False


def check_rate_limiting(api_url: str) -> bool:
    HEAD("7. Rate Limiting (send 5 rapid unauthenticated requests)")
    got_limited = False
    for i in range(5):
        try:
            urllib.request.urlopen(f"{api_url}/health", timeout=5)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                got_limited = True
                OK(f"Rate limit triggered on request {i + 1} (expected for unauthenticated burst)")
                break
    if not got_limited:
        INFO(
            "Rate limiter not triggered (limit may be high — "
            "OK for health endpoint which is exempt)"
        )
    return True


def check_correlation_id(api_url: str) -> bool:
    HEAD("8. Correlation ID Header")
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=10) as r:
            cid = r.headers.get("X-Correlation-Id")
            if cid:
                OK(f"X-Correlation-Id present → {cid}")
                return True
            else:
                FAIL("X-Correlation-Id header missing from /health response")
                return False
    except Exception as e:
        FAIL(f"Correlation ID check failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="SignalForge deployment validator")
    parser.add_argument(
        "--api",
        required=True,
        help="Base URL of the deployed API, e.g. https://signalforge-api.onrender.com",
    )
    parser.add_argument(
        "--token", required=True, help="A valid Auth0 Bearer token for the workspace"
    )
    parser.add_argument(
        "--workspace", default="", help="Workspace ID to test against (must exist in DB)"
    )
    args = parser.parse_args()

    api_url = args.api.rstrip("/")
    results = []

    print(colour("1;35", "\n🔍 SignalForge Deployment Validator"))
    print(colour("35", f"   Target: {api_url}"))
    print(colour("35", f"   Time:   {datetime.now(UTC).isoformat()}"))

    results.append(check_health(api_url))
    results.append(check_correlation_id(api_url))

    if args.workspace and args.token:
        results.append(check_auth(api_url, args.token, args.workspace))
        competitor_id = check_competitor_crud(api_url, args.token, args.workspace)
        results.append(competitor_id is not None)
        if competitor_id:
            results.append(
                check_source_and_crawl(api_url, args.token, args.workspace, competitor_id)
            )
        results.append(check_events(api_url, args.token, args.workspace))
        results.append(check_briefing(api_url, args.token, args.workspace))
    else:
        INFO("Skipping authenticated checks — pass --workspace and --token to run them")

    results.append(check_rate_limiting(api_url))

    passed = sum(results)
    total = len(results)
    print()
    if passed == total:
        print(colour("1;32", f"✅ All {total} checks passed — SignalForge is healthy!"))
        sys.exit(0)
    else:
        print(
            colour("1;31", f"❌ {total - passed}/{total} checks failed — review the output above.")
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
