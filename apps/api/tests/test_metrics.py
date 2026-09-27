from app.main import create_app
from app.metrics import MetricsCollector
from app.settings import Settings
from fastapi.testclient import TestClient


def test_metrics_collector_records_and_formats_prometheus():
    collector = MetricsCollector()
    collector.record_http_request("GET", "/health", 200)
    collector.record_http_request("POST", "/v1/workspaces/ws-1/sources", 201)
    collector.record_crawl_run("succeeded")
    collector.record_classified_event("pricing", "high")

    prom_text = collector.to_prometheus()
    assert "signalforge_uptime_seconds" in prom_text
    assert (
        'signalforge_http_requests_total{method="GET",path="/health",status="200"} 1' in prom_text
    )
    # Verifies dynamic workspace path was normalized to prevent high cardinality
    expected_metric = (
        'signalforge_http_requests_total{method="POST",'
        'path="/v1/workspaces/{workspace_id}/sources",status="201"} 1'
    )
    assert expected_metric in prom_text
    assert 'signalforge_crawl_runs_total{status="succeeded"} 1' in prom_text
    assert 'signalforge_classified_events_total{category="pricing",impact="high"} 1' in prom_text

    data = collector.to_dict()
    assert data["http_requests_total"] == 2
    assert data["crawl_runs"]["succeeded"] == 1
    assert data["classified_events"]["pricing:high"] == 1


def test_metrics_endpoint_integration():
    settings = Settings(
        app_env="test",
        repository_backend="memory",
        queue_backend="memory",
        classifier_backend="fake",
    )
    app = create_app(settings)
    client = TestClient(app)

    # First request
    client.get("/health")

    # Fetch Prometheus metrics
    prom_res = client.get("/metrics")
    assert prom_res.status_code == 200
    assert "text/plain" in prom_res.headers["content-type"]
    assert "signalforge_uptime_seconds" in prom_res.text

    # Fetch JSON metrics
    json_res = client.get("/metrics?format=json")
    assert json_res.status_code == 200
    data = json_res.json()
    assert "uptime_seconds" in data
    assert "http_requests_total" in data
