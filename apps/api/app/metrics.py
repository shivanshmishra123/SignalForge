"""Operational metrics tracking and Prometheus format rendering."""

import time
from collections import Counter
from threading import Lock


class MetricsCollector:
    """Thread-safe operational metrics collector for SignalForge."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._start_time = time.monotonic()
        self._http_requests: Counter[tuple[str, str, int]] = Counter()
        self._crawl_runs: Counter[str] = Counter()
        self._classified_events: Counter[tuple[str, str]] = Counter()

    def record_http_request(self, method: str, path: str, status_code: int) -> None:
        # Normalize dynamic path segments to keep metric cardinality low
        normalized_path = path
        if path.startswith("/v1/workspaces/"):
            parts = path.split("/")
            # e.g., /v1/workspaces/{id}/sources -> /v1/workspaces/{workspace_id}/sources
            if len(parts) >= 4:
                parts[3] = "{workspace_id}"
            if len(parts) >= 6 and parts[4] in {"sources", "runs", "events", "competitors"}:
                parts[5] = "{id}"
            normalized_path = "/".join(parts)

        with self._lock:
            self._http_requests[(method.upper(), normalized_path, status_code)] += 1

    def record_crawl_run(self, status: str) -> None:
        with self._lock:
            self._crawl_runs[status] += 1

    def record_classified_event(self, category: str, impact: str) -> None:
        with self._lock:
            self._classified_events[(category, impact)] += 1

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def to_prometheus(self) -> str:
        """Render metrics in Prometheus exposition text format."""
        with self._lock:
            lines = [
                "# HELP signalforge_uptime_seconds Process uptime in seconds.",
                "# TYPE signalforge_uptime_seconds gauge",
                f"signalforge_uptime_seconds {self.uptime_seconds():.2f}",
                "",
                "# HELP signalforge_http_requests_total Total HTTP requests handled.",
                "# TYPE signalforge_http_requests_total counter",
            ]
            for (method, path, status), count in sorted(self._http_requests.items()):
                metric_labels = f'method="{method}",path="{path}",status="{status}"'
                lines.append(f"signalforge_http_requests_total{{{metric_labels}}} {count}")
            lines.extend(
                [
                    "",
                    "# HELP signalforge_crawl_runs_total Total crawl runs processed.",
                    "# TYPE signalforge_crawl_runs_total counter",
                ]
            )
            for status, count in sorted(self._crawl_runs.items()):
                lines.append(f'signalforge_crawl_runs_total{{status="{status}"}} {count}')
            lines.extend(
                [
                    "",
                    "# HELP signalforge_classified_events_total Total candidate events classified.",
                    "# TYPE signalforge_classified_events_total counter",
                ]
            )
            for (cat, imp), count in sorted(self._classified_events.items()):
                event_labels = f'category="{cat}",impact="{imp}"'
                lines.append(f"signalforge_classified_events_total{{{event_labels}}} {count}")
            lines.append("")
            return "\n".join(lines)

    def to_dict(self) -> dict:
        """Return metrics as a dictionary for JSON reporting."""
        with self._lock:
            return {
                "uptime_seconds": round(self.uptime_seconds(), 2),
                "http_requests_total": sum(self._http_requests.values()),
                "crawl_runs": dict(self._crawl_runs),
                "classified_events": {
                    f"{cat}:{imp}": count for (cat, imp), count in self._classified_events.items()
                },
            }


_global_metrics = MetricsCollector()


def get_metrics_collector() -> MetricsCollector:
    return _global_metrics
