import pytest
from app.settings import get_settings


@pytest.fixture(autouse=True)
def isolate_unit_test_environment(monkeypatch, request):
    # Only isolate if not explicitly running docker integration tests
    if "test_docker_integration" in request.node.nodeid:
        yield
        return

    monkeypatch.setenv("REPOSITORY_BACKEND", "memory")
    monkeypatch.setenv("QUEUE_BACKEND", "memory")
    monkeypatch.setenv("CLASSIFIER_BACKEND", "fake")
    monkeypatch.setenv("SLACK_BACKEND", "fake")
    monkeypatch.setenv("SLACK_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AUTH_MODE", "development")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
