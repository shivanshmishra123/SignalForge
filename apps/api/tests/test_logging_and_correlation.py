import json
import logging
from uuid import UUID

from app.logging_config import (
    CorrelationMiddleware,
    JsonFormatter,
    get_correlation_id,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    @app.get("/ping")
    async def ping():
        return {"cid": get_correlation_id()}

    return app


def test_correlation_id_generated_when_missing():
    app = _make_app()
    client = TestClient(app)

    response = client.get("/ping")
    assert response.status_code == 200
    cid_header = response.headers.get("X-Correlation-Id")
    assert cid_header is not None
    # Verify it is a valid UUID
    UUID(cid_header)
    assert response.json()["cid"] == cid_header


def test_correlation_id_propagated_from_header():
    app = _make_app()
    client = TestClient(app)

    custom_cid = "custom-trace-12345"
    response = client.get("/ping", headers={"X-Correlation-Id": custom_cid})
    assert response.status_code == 200
    assert response.headers.get("X-Correlation-Id") == custom_cid
    assert response.json()["cid"] == custom_cid


def test_json_formatter_outputs_valid_json_with_metadata():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=42,
        msg="Test event %s",
        args=("occurred",),
        exc_info=None,
    )
    record.correlation_id = "test-corr-id"

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["msg"] == "Test event occurred"
    assert data["logger"] == "test.logger"
    assert data["correlation_id"] == "test-corr-id"
    assert "ts" in data
