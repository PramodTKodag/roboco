"""Policy-forbidden integrations stay mounted but can neither store nor reach out."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from roboco.api.app import create_app

_WRITES = [
    ("PUT", "/api/providers/ollama-key"),
    ("PUT", "/api/providers/grok-key"),
    ("PUT", "/api/providers/zai-key"),
    ("PUT", "/api/providers/nebius-key"),
    ("PUT", "/api/providers/self-hosted"),
    ("POST", "/api/providers/self-hosted/test"),
    ("POST", "/api/x/credentials"),
    ("POST", "/api/telegram/credentials"),
    ("PUT", "/api/github-app/credentials"),
    ("DELETE", "/api/github-app/credentials"),
    ("POST", "/api/video/request"),
]
_ANY_METHOD = [
    "/api/providers/nebius/models",
    "/api/providers/self-hosted/models",
    "/api/github-app/installations",
    "/api/github-app/installations/1/repositories",
    "/api/video/preview/00000000-0000-0000-0000-000000000001/index.html",
]


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.mark.parametrize(("method", "path"), _WRITES)
def test_state_changing_calls_are_refused(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path, json={})
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert "subscription-only policy" in response.json()["error"]["message"]


@pytest.mark.parametrize("path", _ANY_METHOD)
def test_outbound_or_active_content_routes_are_refused_even_for_reads(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == HTTPStatus.FORBIDDEN
    assert "subscription-only policy" in response.json()["error"]["message"]
