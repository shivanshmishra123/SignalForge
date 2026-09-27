"""Structured JSON logging and request correlation ID middleware."""

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# Context variable so correlation ID is available anywhere in a request's call stack
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def get_correlation_id() -> str:
    return _correlation_id.get()


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = get_correlation_id()
        if cid:
            payload["correlation_id"] = cid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Copy any extra fields the caller passed via LogRecord
        for key, value in record.__dict__.items():
            if key not in {
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "name",
                "taskName",
                "message",
            } and not key.startswith("_"):
                try:
                    json.dumps(value)  # only include JSON-serialisable extras
                    payload[key] = value
                except (TypeError, ValueError):
                    pass
        return json.dumps(payload, default=str)


def configure_logging(log_level: str = "INFO", force_json: bool = False) -> None:
    """Replace the root handler with a JSON handler when running in production."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    if force_json or log_level.upper() != "DEBUG":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.handlers = [handler]

    # Suppress noisy uvicorn access logs — we log them ourselves in the middleware
    logging.getLogger("uvicorn.access").propagate = False


class CorrelationMiddleware(BaseHTTPMiddleware):
    """
    Assigns a correlation ID to every request.
    - Reads the incoming ``X-Correlation-Id`` header when the caller provides one.
    - Otherwise generates a new UUID.
    - Always echoes the ID back in the response header.
    - Logs a structured access record with method, path, status, and duration.
    """

    _logger = logging.getLogger("signalforge.access")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
        token = _correlation_id.set(cid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            _correlation_id.reset(token)

        response.headers["X-Correlation-Id"] = cid
        try:
            from app.metrics import get_metrics_collector

            get_metrics_collector().record_http_request(
                request.method, request.url.path, response.status_code
            )
        except Exception:
            pass
        self._logger.info(
            "%s %s %d",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "correlation_id": cid,
            },
        )
        return response
