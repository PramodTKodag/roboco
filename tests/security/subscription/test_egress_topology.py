"""The compose network boundary, probed from the runtime and worker namespaces.

Synthetic mode: nothing here contacts a real provider. Only disposable resources
under a unique compose project name are created and removed.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import time
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

SERVICE_IMAGE = "roboco-personal-claude-service:itest"
WORKER_IMAGE = "roboco-personal-worker:itest"
COMPOSE_FILE = REPO_ROOT / "compose.personal.yml"
DIGEST_FILE = REPO_ROOT / "deploy" / "personal" / "egress-policy.sha256"


def _unavailable() -> str | None:
    if (reason := docker_unavailable_reason()) is not None:
        return reason
    for image in (SERVICE_IMAGE, WORKER_IMAGE):
        if docker("image", "inspect", image, check=False).returncode != 0:
            return f"image not built locally: {image}"
    return None


_UNAVAILABLE = _unavailable()
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_UNAVAILABLE is not None, reason=_UNAVAILABLE or "available"),
]

_spec = importlib.util.spec_from_file_location(
    "verify_network", REPO_ROOT / "scripts" / "personal" / "verify_network.py"
)
assert _spec is not None and _spec.loader is not None
verify_network = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("verify_network", verify_network)
_spec.loader.exec_module(verify_network)


# The compose file requires every deployment secret to be set before it will even
# render. Only the two egress services are started here, so these placeholders are
# never used by a running container.
_REQUIRED_COMPOSE_VARIABLES = frozenset(
    re.findall(r"\$\{(\w+):\?", COMPOSE_FILE.read_text())
) - {"ROBOCO_EGRESS_POLICY_SHA256"}
_SYNTHETIC_COMPOSE_VARIABLES = {
    name: f"synthetic-{name.lower()}" for name in sorted(_REQUIRED_COMPOSE_VARIABLES)
}
_EGRESS_SERVICES = ("egress-proxy", "egress-ca-publish")


def _compose(
    project: str, *args: str, digest: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        **_SYNTHETIC_COMPOSE_VARIABLES,
        "ROBOCO_EGRESS_POLICY_SHA256": digest or DIGEST_FILE.read_text().strip(),
    }
    return subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
    )


def _start(project: str) -> None:
    started = _compose(project, "up", "-d", *_EGRESS_SERVICES)
    assert started.returncode == 0, started.stderr
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        health = docker(
            "inspect", "--format", "{{.State.Health.Status}}",
            f"{project}-egress-proxy-1", check=False,
        ).stdout.strip()  # fmt: skip
        published = docker(
            "inspect", "--format", "{{.State.ExitCode}} {{.State.Running}}",
            f"{project}-egress-ca-publish-1", check=False,
        ).stdout.strip()  # fmt: skip
        if health == "healthy" and published == "0 false":
            return
        time.sleep(1)
    raise AssertionError("egress stack did not become ready")


@pytest.fixture
def project() -> Iterator[str]:
    name = f"roboco-egress-itest-{uuid4().hex[:6]}"
    try:
        yield name
    finally:
        _compose(name, "down", "--remove-orphans")
        docker(
            "volume",
            "rm",
            f"{name}_egress_ca_private",
            f"{name}_egress_ca_public",
            check=False,
        )


def test_compose_networks_are_isolated_as_designed(project: str) -> None:
    _start(project)
    inspected = docker(
        "network",
        "inspect",
        f"{project}_runtime",
        f"{project}_egress",
        "--format",
        "{{.Name}} internal={{.Internal}} ipv6={{.EnableIPv6}}",
    )
    assert f"{project}_runtime internal=true ipv6=false" in inspected.stdout
    assert f"{project}_egress internal=false ipv6=false" in inspected.stdout
    attached = docker(
        "inspect",
        "--format",
        "{{range $n, $_ := .NetworkSettings.Networks}}{{$n}} {{end}}",
        f"{project}-egress-proxy-1",
    ).stdout.split()
    assert sorted(attached) == [f"{project}_egress", f"{project}_runtime"]


def test_runtime_and_worker_namespaces_have_no_route_around_the_proxy(
    project: str,
) -> None:
    _start(project)
    results = verify_network.run_checks(project, SERVICE_IMAGE, WORKER_IMAGE)
    failed = [result for result in results if not result.passed]
    assert not failed, failed
    assert len(results) >= 15  # noqa: PLR2004 - every named check ran


def test_the_stack_refuses_to_start_without_the_reviewed_digest(project: str) -> None:
    # `up` itself reports failure: the publisher waits for a healthy proxy.
    _compose(project, "up", "-d", *_EGRESS_SERVICES, digest="0" * 64)
    proxy = f"{project}-egress-proxy-1"
    deadline = time.monotonic() + 30
    logs = ""
    while time.monotonic() < deadline and "egress policy rejected" not in logs:
        time.sleep(1)
        result = docker("logs", proxy, check=False)
        logs = result.stdout + result.stderr
    assert "egress policy rejected: policy_digest_mismatch" in logs
    assert "HTTP(S) proxy listening" not in logs
    health = docker("inspect", "--format", "{{.State.Health.Status}}", proxy).stdout
    assert health.strip() != "healthy"
