from app.rate_limit import RateLimitMiddleware
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _make_app(rpm: int = 2, burst: int = 2) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rpm, burst=burst)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics():
        return {"metrics": "ok"}

    @app.get("/test")
    async def test_endpoint():
        return {"data": "success"}

    @app.get("/user-test")
    async def user_test(request: Request):
        return {"user": getattr(getattr(request.state, "principal", None), "user_id", None)}

    return app


def test_rate_limiter_allows_requests_under_limit():
    app = _make_app(rpm=10, burst=10)
    client = TestClient(app)

    res1 = client.get("/test")
    assert res1.status_code == 200
    assert res1.json() == {"data": "success"}

    res2 = client.get("/test")
    assert res2.status_code == 200


def test_rate_limiter_blocks_burst_and_returns_429():
    app = _make_app(rpm=2, burst=2)
    client = TestClient(app)

    # 2 allowed
    r1 = client.get("/test")
    r2 = client.get("/test")
    assert r1.status_code == 200
    assert r2.status_code == 200

    # 3rd is blocked
    r3 = client.get("/test")
    assert r3.status_code == 429
    assert r3.json()["detail"]["code"] == "RATE_LIMITED"
    assert "Retry-After" in r3.headers
    assert r3.headers["X-RateLimit-Limit"] == "2"


def test_rate_limiter_exempts_health_ready_and_metrics():
    app = _make_app(rpm=1, burst=1)
    client = TestClient(app)

    # Health, ready, metrics are never blocked even after exhausting bucket
    r_test = client.get("/test")
    assert r_test.status_code == 200

    r_test2 = client.get("/test")
    assert r_test2.status_code == 429

    for _ in range(5):
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/metrics").status_code == 200


def test_rate_limiter_differentiates_forwarded_ip():
    app = _make_app(rpm=1, burst=1)
    client = TestClient(app)

    # First IP exhausts its token
    r1 = client.get("/test", headers={"X-Forwarded-For": "198.51.100.1"})
    assert r1.status_code == 200

    r1_blocked = client.get("/test", headers={"X-Forwarded-For": "198.51.100.1"})
    assert r1_blocked.status_code == 429

    # Different IP has its own bucket
    r2 = client.get("/test", headers={"X-Forwarded-For": "198.51.100.2"})
    assert r2.status_code == 200
