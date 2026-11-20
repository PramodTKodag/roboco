"""The personal edge (deploy/personal/nginx.conf) in a real nginx container.

Synthetic mode: three throwaway echo backends stand in for the panel, API and
dispatcher on a disposable network. Nothing here reaches a real service.
"""

from __future__ import annotations

import http.client
import json
import time
from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from tests.integration.subscription.docker_support import (
    REPO_ROOT,
    docker,
    docker_unavailable_reason,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

NGINX_IMAGE = (
    "nginx@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de"
)
ECHO_IMAGE = "python:3.13-slim-bookworm"
PERSONAL = REPO_ROOT / "deploy" / "personal"
EDGE_PORT = "3300"
OWNER_HOST = f"localhost:{EDGE_PORT}"

_ECHO_SERVER = (
    "import json,sys\n"
    "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
    "NAME=sys.argv[1]\n"
    "class H(BaseHTTPRequestHandler):\n"
    "    def do_GET(self):\n"
    "        body=json.dumps({'backend':NAME,'path':self.path,"
    "'headers':{k.lower():v for k,v in self.headers.items()}}).encode()\n"
    "        self.send_response(200)\n"
    "        self.send_header('Content-Type','application/json')\n"
    "        self.send_header('Content-Length',str(len(body)))\n"
    "        self.end_headers()\n"
    "        self.wfile.write(body)\n"
    "    def log_message(self,*a): pass\n"
    "HTTPServer(('0.0.0.0',int(sys.argv[2])),H).serve_forever()\n"
)


def _unavailable() -> str | None:
    if (reason := docker_unavailable_reason()) is not None:
        return reason
    for image in (NGINX_IMAGE, ECHO_IMAGE):
        if docker("image", "inspect", image, check=False).returncode != 0:
            return f"image not present locally: {image}"
    return None


_UNAVAILABLE = _unavailable()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_UNAVAILABLE is not None, reason=_UNAVAILABLE or "available"),
]


@pytest.fixture(scope="module")
def edge_port() -> Iterator[int]:
    tag = uuid4().hex[:8]
    network = f"roboco-edge-test-{tag}"
    containers: list[str] = []
    docker("network", "create", network)
    try:
        for alias, port in (
            ("panel", 3000),
            ("orchestrator", 8000),
            ("dispatcher", 8000),
        ):
            name = f"roboco-edge-{alias}-{tag}"
            containers.append(name)
            docker(
                "run", "-d", "--rm", "--name", name, "--network", network,
                "--network-alias", alias, ECHO_IMAGE,
                "python", "-c", _ECHO_SERVER, alias, str(port),
            )  # fmt: skip
        edge = f"roboco-edge-nginx-{tag}"
        containers.append(edge)
        docker(
            "run",
            "-d",
            "--rm",
            "--name",
            edge,
            "--network",
            network,
            "-p",
            "127.0.0.1::80",
            "-e",
            f"ROBOCO_PERSONAL_PORT={EDGE_PORT}",
            "-e",
            "NGINX_ENVSUBST_FILTER=^ROBOCO_",
            "-v",
            f"{PERSONAL / 'nginx.conf'}:/etc/nginx/templates/default.conf.template:ro",
            "-v",
            f"{PERSONAL / 'nginx-proxy-common.conf'}"
            ":/etc/nginx/personal/nginx-proxy-common.conf:ro",
            NGINX_IMAGE,
        )
        mapping = docker("port", edge, "80/tcp").stdout.split(":")[-1].strip()
        port = int(mapping)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if _get(port, "/health").status == HTTPStatus.OK:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail(docker("logs", edge, check=False).stderr)
        yield port
    finally:
        for name in containers:
            docker("rm", "-f", name, check=False)
        docker("network", "rm", network, check=False)


def _get(
    port: int,
    path: str,
    *,
    host: str = OWNER_HOST,
    headers: dict[str, str] | None = None,
) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path, headers={"Host": host, **(headers or {})})
    response = conn.getresponse()
    response.body = response.read()  # type: ignore[attr-defined]
    return response


def _echo(response: http.client.HTTPResponse) -> dict[str, object]:
    return json.loads(response.body)  # type: ignore[attr-defined,no-any-return]


def test_routes_reach_the_expected_backend(edge_port: int) -> None:
    expected = {
        "/": "panel",
        "/api/tasks": "orchestrator",
        "/ws/stream": "orchestrator",
        "/api/orchestrator/fleet": "dispatcher",
        "/api/secretary/live/start": "dispatcher",
        "/api/prompter/live/events": "dispatcher",
    }
    for path, backend in expected.items():
        assert _echo(_get(edge_port, path))["backend"] == backend, path


def test_other_hosts_are_refused(edge_port: int) -> None:
    for host in ("localhost:3001", "localhost", "127.0.0.1:8000"):
        assert (
            _get(edge_port, "/api/tasks", host=host).status
            == HTTPStatus.MISDIRECTED_REQUEST
        ), host
    with pytest.raises(http.client.RemoteDisconnected):
        _get(edge_port, "/api/tasks", host="evil.example")


def test_browser_identity_headers_never_reach_the_api(edge_port: int) -> None:
    forged = {
        "X-Agent-Id": "00000000-0000-0000-0000-000000000001",
        "X-Agent-Role": "ceo",
        "X-Agent-Team": "backend",
        "X-Agent-Token": "forged-token",
        "X-Forwarded-For": "10.9.9.9",
    }
    seen = _echo(_get(edge_port, "/api/tasks", headers=forged))["headers"]
    assert isinstance(seen, dict)
    for name in ("x-agent-id", "x-agent-role", "x-agent-team", "x-agent-token"):
        assert not seen.get(name), name
    assert seen["host"] == OWNER_HOST
    assert "10.9.9.9" not in str(seen["x-forwarded-for"])


def test_panel_responses_carry_a_restrictive_policy(edge_port: int) -> None:
    response = _get(edge_port, "/")
    csp = response.getheader("Content-Security-Policy") or ""
    for directive in (
        "default-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "form-action 'self'",
    ):
        assert directive in csp, directive
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    assert response.getheader("X-Frame-Options") == "DENY"
    assert response.getheader("Referrer-Policy") == "no-referrer"
    assert "nginx/" not in (response.getheader("Server") or "")


def test_scanner_paths_are_dropped(edge_port: int) -> None:
    with pytest.raises(http.client.RemoteDisconnected):
        _get(edge_port, "/.env")
