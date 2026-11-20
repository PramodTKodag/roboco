"""The pinned TLS proxy with the shipped addon and policy, on an internal network.

Synthetic mode: no real provider is contacted. The test network is internal (no
route out and no outside DNS), so an allowed request reaches the dial step and
fails there; a policy denial answers 403 before any dial. Both are distinct and
are told apart by status and by the proxy's own decision log.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import textwrap
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from tests.integration.subscription.docker_support import (
    REPO_ROOT,
    docker,
    docker_unavailable_reason,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

PROXY_IMAGE = (
    "mitmproxy/mitmproxy@sha256:"
    "743b6cdc817211d64bc269f5defacca8d14e76e647fc474e5c7244dbcb645141"
)
CLIENT_IMAGE = (
    "python@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72"
)
LABEL = "roboco.personal.egress-test"
DEPLOY = REPO_ROOT / "deploy" / "personal"
POLICY_MODULE = REPO_ROOT / "roboco" / "runtime" / "subscription" / "egress_policy.py"
CANARY = "synthetic-egress-canary-7c1e"
EXIT_POLICY_INVALID = 78
FORWARD_FAILURE = 502


def _unavailable() -> str | None:
    if (reason := docker_unavailable_reason()) is not None:
        return reason
    for image in (PROXY_IMAGE, CLIENT_IMAGE):
        if docker("image", "inspect", image, check=False).returncode != 0:
            return f"image not present locally: {image}"
    return None


_UNAVAILABLE = _unavailable()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_UNAVAILABLE is not None, reason=_UNAVAILABLE or "available"),
]

_CLIENT_SCRIPT = textwrap.dedent(
    """
    import http.client, json, socket, ssl, sys

    proxy, canary = sys.argv[1], sys.argv[2]
    context = ssl.create_default_context(cafile="/ca/mitmproxy-ca-cert.pem")
    results = {}

    def through_proxy(name, host, method, target, host_header=None):
        try:
            conn = http.client.HTTPSConnection(
                proxy, 8080, context=context, timeout=15
            )
            conn.set_tunnel(host, 443)
            conn.putrequest(method, target, skip_host=True)
            conn.putheader("Host", host_header or host)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("X-Canary", canary)
            body = json.dumps({"canary": canary}).encode()
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            response = conn.getresponse()
            results[name] = [response.status, response.read().decode()]
        except OSError as exc:
            results[name] = ["error", type(exc).__name__ + ": " + str(exc)[:80]]

    api = "api.anthropic.com"
    messages = "/v1/messages?beta=true"
    through_proxy("telemetry", api, "POST", "/api/event_logging/v2/batch")
    through_proxy("settings", api, "GET", "/api/claude_code/settings")
    through_proxy("wrong_method", api, "GET", messages)
    through_proxy("other_host", "evil.example", "POST", messages)
    through_proxy("ip_literal", "93.184.216.34", "POST", messages)
    through_proxy(
        "authority_mismatch", api, "POST", messages, host_header="evil.example"
    )
    through_proxy("approved_shape", api, "POST", messages)

    direct_targets = (
        ("direct_ip", ("1.1.1.1", 443)),
        ("direct_name", ("example.com", 443)),
    )
    for name, address in direct_targets:
        try:
            socket.create_connection(address, timeout=5).close()
            results[name] = "connected"
        except OSError as exc:
            results[name] = type(exc).__name__
    print(json.dumps(results))
    """
)


class ProxyStack:
    def __init__(self, workdir: Path) -> None:
        suffix = uuid4().hex[:8]
        self.network = f"roboco-egress-test-{suffix}"
        self.proxy = f"roboco-egress-proxy-{suffix}"
        self.confdir = workdir / "conf"
        self.confdir.mkdir()
        self.confdir.chmod(0o777)
        self.assets = workdir / "assets"
        self.assets.mkdir()
        shutil.copy(DEPLOY / "proxy_addon.py", self.assets)
        shutil.copy(POLICY_MODULE, self.assets)
        shutil.copy(DEPLOY / "egress-policy.json", self.assets)
        self.digest = (DEPLOY / "egress-policy.sha256").read_text().strip()
        docker("network", "create", "--internal", "--label", LABEL, self.network)

    def start_proxy(
        self,
        *,
        digest: str | None = None,
        detach: bool = True,
        profile: str = "normal",
    ) -> Any:
        args = [
            "run",
            "--name",
            self.proxy,
            "--label",
            LABEL,
            "--network",
            self.network,
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp",
            "-v",
            f"{self.confdir}:/confdir",
            "-v",
            f"{self.assets}:/opt/egress:ro",
            "-v",
            f"{DEPLOY / 'proxy.yaml'}:/confdir/config.yaml:ro",
            "-e",
            "ROBOCO_EGRESS_POLICY_PATH=/opt/egress/egress-policy.json",
            "-e",
            f"ROBOCO_EGRESS_POLICY_SHA256={digest or self.digest}",
            "-e",
            f"ROBOCO_EGRESS_PROFILE={profile}",
            "-e",
            "PYTHONPATH=/opt/egress",
            "-e",
            "PYTHONUNBUFFERED=1",
            "--entrypoint",
            "mitmdump",
            *(["--detach"] if detach else []),
            PROXY_IMAGE,
            "--set",
            "confdir=/confdir",
        ]
        return docker(*args, check=False)

    def wait_until_ready(self) -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            logs = docker("logs", self.proxy, check=False)
            if (self.confdir / "mitmproxy-ca-cert.pem").exists() and (
                "listening" in (logs.stdout + logs.stderr).lower()
            ):
                return
            time.sleep(0.5)
        raise AssertionError("proxy did not become ready")

    def run_client(self) -> dict[str, Any]:
        result = docker(
            "run", "--rm", "--label", LABEL, "--network", self.network,
            "--read-only", "--cap-drop", "ALL",
            "-v", f"{self.confdir}:/ca:ro",
            CLIENT_IMAGE,
            "python", "-c", _CLIENT_SCRIPT, self.proxy, CANARY,
        )  # fmt: skip
        outcomes: dict[str, Any] = json.loads(result.stdout)
        return outcomes

    def proxy_logs(self) -> str:
        logs = docker("logs", self.proxy, check=False)
        return logs.stdout + logs.stderr

    def close(self) -> None:
        docker("rm", "--force", self.proxy, check=False)
        docker("network", "rm", self.network, check=False)


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[ProxyStack]:
    built = ProxyStack(tmp_path)
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def results(stack: ProxyStack) -> Iterator[dict[str, Any]]:
    started = stack.start_proxy()
    assert started.returncode == 0, started.stderr
    stack.wait_until_ready()
    yield stack.run_client()


def _body_error(entry: list[Any]) -> str:
    error: str = json.loads(entry[1])["error"]
    return error


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("telemetry", "path_not_allowed"),
        ("settings", "path_not_allowed"),
        ("wrong_method", "method_not_allowed"),
        ("authority_mismatch", "authority_mismatch"),
    ],
)
def test_denied_requests_get_a_policy_refusal(
    results: dict[str, Any], case: str, reason: str
) -> None:
    status, _ = results[case]
    assert status == HTTPStatus.FORBIDDEN
    assert _body_error(results[case]) == reason


@pytest.mark.parametrize("case", ["other_host", "ip_literal"])
def test_tunnels_to_unapproved_destinations_are_refused(
    results: dict[str, Any], case: str
) -> None:
    assert results[case][0] == "error"
    assert "403" in results[case][1]


def test_the_approved_request_passes_policy_and_stops_at_the_dial(
    stack: ProxyStack, results: dict[str, Any]
) -> None:
    status, _ = results["approved_shape"]
    assert status == FORWARD_FAILURE
    logs = stack.proxy_logs()
    assert "egress allow request POST endpoint=inference_messages" in logs
    assert "reason=resolution_refused" in logs


def test_the_runtime_network_has_no_direct_route_or_outside_dns(
    results: dict[str, Any],
) -> None:
    assert results["direct_ip"] != "connected"
    assert results["direct_name"] != "connected"


def test_proxy_logs_carry_no_request_content(
    stack: ProxyStack, results: dict[str, Any]
) -> None:
    del results
    logs = stack.proxy_logs()
    for forbidden in (CANARY, "evil.example", "/v1/messages", "event_logging", "?beta"):
        assert forbidden not in logs


def test_stopping_the_proxy_blocks_all_traffic(stack: ProxyStack) -> None:
    assert stack.start_proxy().returncode == 0
    stack.wait_until_ready()
    docker("stop", "--time", "2", stack.proxy)
    outcome = subprocess.run(
        ["docker", "run", "--rm", "--label", LABEL, "--network", stack.network,
         CLIENT_IMAGE, "python", "-c",
         "import socket,sys\n"
         "try:\n"
         f" socket.create_connection(('{stack.proxy}', 8080), timeout=5)\n"
         " print('connected')\n"
         "except OSError as e:\n"
         " print(type(e).__name__)\n"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert outcome.stdout.strip() != "connected"


@pytest.mark.parametrize(
    "digest", ["0" * 64, ""], ids=["unreviewed-digest", "missing-digest"]
)
def test_the_proxy_refuses_to_start_with_an_unreviewed_policy(
    stack: ProxyStack, digest: str
) -> None:
    stack.start_proxy(digest=digest or "x", detach=False)
    state = docker("inspect", "--format", "{{.State.ExitCode}}", stack.proxy)
    assert int(state.stdout.strip()) == EXIT_POLICY_INVALID


def test_the_proxy_refuses_to_start_with_an_empty_policy(stack: ProxyStack) -> None:
    (stack.assets / "egress-policy.json").write_text("")
    stack.start_proxy(digest="0" * 64, detach=False)
    state = docker("inspect", "--format", "{{.State.ExitCode}}", stack.proxy)
    assert int(state.stdout.strip()) == EXIT_POLICY_INVALID


def _approve_in_maintenance(stack: ProxyStack, host: str) -> str:
    """Write a copy of the policy whose maintenance profile allows one host."""
    policy = json.loads((stack.assets / "egress-policy.json").read_text())
    endpoint = dict(policy["profiles"]["normal"][0], id="login_probe", host=host)
    policy["profiles"]["maintenance"] = [endpoint]
    text = json.dumps(policy)
    (stack.assets / "egress-policy.json").write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def test_maintenance_logs_the_refused_target_without_query_or_content(
    stack: ProxyStack,
) -> None:
    digest = _approve_in_maintenance(stack, "api.anthropic.com")
    started = stack.start_proxy(digest=digest, profile="maintenance")
    assert started.returncode == 0, started.stderr
    stack.wait_until_ready()
    stack.run_client()
    logs = stack.proxy_logs()
    assert "target=evil.example:443" in logs
    assert "target=api.anthropic.com:443/api/event_logging/v2/batch" in logs
    for forbidden in (CANARY, "beta", "?"):
        assert forbidden not in "\n".join(
            line for line in logs.splitlines() if "target=" in line
        ), forbidden


def test_an_empty_maintenance_profile_denies_everything_and_names_the_host(
    stack: ProxyStack,
) -> None:
    started = stack.start_proxy(profile="maintenance")
    assert started.returncode == 0, started.stderr
    stack.wait_until_ready()
    outcomes = stack.run_client()
    assert outcomes["approved_shape"][0] == "error"
    logs = stack.proxy_logs()
    assert "egress deny connect" in logs
    assert "target=api.anthropic.com:443" in logs
    assert "egress allow" not in logs
