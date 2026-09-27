from app.main import create_app
from fastapi.testclient import TestClient

ADMIN_HEADERS = {
    "X-User-Id": "user-a",
    "X-Workspace-Id": "workspace-a",
    "X-Role": "admin",
}


def test_competitor_and_source_crud_is_tenant_scoped() -> None:
    client = TestClient(create_app())
    competitor = client.post(
        "/v1/workspaces/workspace-a/competitors",
        headers=ADMIN_HEADERS,
        json={"name": "Acme", "canonical_domain": "acme.example"},
    )
    assert competitor.status_code == 201

    source = client.post(
        "/v1/workspaces/workspace-a/sources",
        headers=ADMIN_HEADERS,
        json={
            "competitor_id": competitor.json()["id"],
            "source_type": "html",
            "url": "https://acme.example/pricing/",
        },
    )
    assert source.status_code == 201
    assert source.json()["normalized_url"] == "https://acme.example/pricing"

    other_workspace = client.get(
        "/v1/workspaces/workspace-b/sources",
        headers={**ADMIN_HEADERS, "X-Workspace-Id": "workspace-b"},
    )
    assert other_workspace.status_code == 200
    assert other_workspace.json() == []


def test_duplicate_normalized_sources_are_rejected() -> None:
    client = TestClient(create_app())
    competitor = client.post(
        "/v1/workspaces/workspace-a/competitors",
        headers=ADMIN_HEADERS,
        json={"name": "Acme", "canonical_domain": "acme.example"},
    )
    competitor_id = competitor.json()["id"]
    payload = {
        "competitor_id": competitor_id,
        "source_type": "rss",
        "url": "https://acme.example/news/?utm_source=test",
    }
    assert (
        client.post(
            "/v1/workspaces/workspace-a/sources", headers=ADMIN_HEADERS, json=payload
        ).status_code
        == 201
    )
    duplicate = client.post(
        "/v1/workspaces/workspace-a/sources",
        headers=ADMIN_HEADERS,
        json={**payload, "url": "https://ACME.example/news/"},
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "DUPLICATE_SOURCE"


def test_source_policy_and_role_are_enforced() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/v1/workspaces/workspace-a/competitors",
        headers={**ADMIN_HEADERS, "X-Role": "viewer"},
        json={"name": "Acme", "canonical_domain": "acme.example"},
    )
    assert response.status_code == 403

    invalid = client.post(
        "/v1/workspaces/workspace-a/sources",
        headers=ADMIN_HEADERS,
        json={
            "competitor_id": "missing",
            "source_type": "html",
            "url": "ftp://acme.example/pricing",
        },
    )
    assert invalid.status_code == 422
