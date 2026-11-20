"""The real service stack against a synthetic provider, inside the built image.

Container-level evidence. The official pinned CLI, the tool gateway, the stdio
bridge and the service are the production code; the provider is a synthetic
fake on an internal-only network, the subscription inspector is substituted
(no login exists here) and the worker executor records jobs. These tests show the
tool and configuration surface, not OAuth behavior: that needs the owner login.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any

import pytest

from tests.integration.subscription.docker_support import (
    REPO_ROOT,
    docker,
    docker_unavailable_reason,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_PROBE_PATH = REPO_ROOT / "scripts" / "personal" / "probe_cli_contract.py"
_spec = importlib.util.spec_from_file_location("probe_cli_contract", _PROBE_PATH)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("probe_cli_contract", probe)
_spec.loader.exec_module(probe)

_FIXTURES = REPO_ROOT / "tests" / "security" / "subscription" / "fixtures"
SERVICE_IMAGE = "roboco-personal-claude-service:itest"
LABEL = "roboco.personal.probe"
SENTINEL = "synthetic-credential-sentinel-9d2c41"
TOOL_PREFIX = "mcp__roboco_worker__"
# One initial request plus at most two CLI retries.
MAX_MODEL_ATTEMPTS = 3
# The CLI lists this inert stub in its request next to deferred MCP tools; the
# service's init-event check and the "cannot run" tests below cover its behavior.
_ADVERTISED_PLACEHOLDERS = frozenset({"DeferredToolPlaceholder"})
_UNAVAILABLE = docker_unavailable_reason() or probe.container_layer_unavailable_reason()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_UNAVAILABLE is not None, reason=_UNAVAILABLE or "available"),
]


@pytest.fixture(scope="session")
def service_image() -> str:
    arch = probe.host_arch()
    docker(
        "build",
        "--quiet",
        "--file",
        str(REPO_ROOT / "docker" / "claude-subscription.Dockerfile"),
        "--build-context",
        f"cli={probe.staged_binary_path(arch).parent}",
        "--build-arg",
        f"CLAUDE_CLI_SHA256={probe.PINNED_BINARY_SHA256[arch]}",
        "--tag",
        SERVICE_IMAGE,
        str(REPO_ROOT),
    )
    return SERVICE_IMAGE


class Stack:
    """An internal-only network, a synthetic provider and one driver run."""

    def __init__(self, image: str, **provider_env: str) -> None:
        self.image = image
        self.id = uuid.uuid4().hex[:10]
        self.provider_env = {"FAKE_SENTINEL": SENTINEL, **provider_env}

    @property
    def network(self) -> str:
        return f"roboco-sub-svc-{self.id}-net"

    @property
    def provider(self) -> str:
        return f"roboco-sub-svc-{self.id}-fake"

    def __enter__(self) -> Stack:
        created = docker(
            "network",
            "create",
            "--internal",
            "--label",
            f"{LABEL}={self.id}",
            self.network,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        env_args: list[str] = []
        for key, value in self.provider_env.items():
            env_args += ["-e", f"{key}={value}"]
        started = docker(
            "run",
            "-d",
            "--name",
            self.provider,
            "--label",
            f"{LABEL}={self.id}",
            "--network",
            self.network,
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "-v",
            f"{_FIXTURES / 'fake_provider.py'}:/srv/fake_provider.py:ro",
            *env_args,
            probe.FAKE_PROVIDER_IMAGE,
            "python",
            "-I",
            "/srv/fake_provider.py",
            check=False,
        )
        assert started.returncode == 0, started.stderr
        for _ in range(50):
            if '"kind": "ready"' in docker("logs", self.provider).stdout:
                return self
            time.sleep(0.2)
        raise AssertionError("synthetic provider did not become ready")

    def __exit__(self, *_: object) -> None:
        docker("rm", "--force", self.provider, check=False)
        docker("network", "rm", self.network, check=False)

    def drive(self, **options: Any) -> dict[str, Any]:
        payload = {
            "base_url": f"http://{self.provider}:8080",
            "operations": ["read_file", "list_files"],
            "purpose": "delivery",
            **options,
        }
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--label",
                f"{LABEL}={self.id}",
                "--network",
                self.network,
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "256",
                "--memory",
                "1g",
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/home/claude:uid=10002,gid=10002",
                "--tmpfs",
                "/run/roboco:uid=10002,gid=10002,mode=0700",
                "-v",
                f"{_FIXTURES / 'service_driver.py'}:/opt/driver/service_driver.py:ro",
                "--entrypoint",
                "python",
                self.image,
                "-P",
                "/opt/driver/service_driver.py",
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        summary: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
        return summary

    def provider_events(self) -> list[dict[str, Any]]:
        logs = docker("logs", self.provider)
        return [
            json.loads(line.removeprefix("PROBE "))
            for line in (logs.stdout + logs.stderr).splitlines()
            if line.startswith("PROBE ")
        ]

    def model_requests(self) -> list[dict[str, Any]]:
        return [e for e in self.provider_events() if e.get("kind") == "messages"]


def _call(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}|{json.dumps(arguments)}"


@pytest.fixture
def stack_factory(service_image: str) -> Iterator[Any]:
    stacks: list[Stack] = []

    def make(**provider_env: str) -> Stack:
        stack = Stack(service_image, **provider_env)
        stack.__enter__()
        stacks.append(stack)
        return stack

    yield make
    for stack in stacks:
        stack.__exit__()


def test_granted_tool_call_reaches_the_worker_bound_to_the_registered_run(
    stack_factory: Any,
) -> None:
    stack = stack_factory(
        FAKE_TOOL_CALL=_call(f"{TOOL_PREFIX}read_file", {"path": "src/app.py"})
    )
    summary = stack.drive()
    assert summary["state"] == "completed", summary["state"]
    (job,) = summary["jobs"]
    assert job["operation"] == "read_file"
    assert job["payload"]["path"] == "src/app.py"
    assert job["run_matches"] and job["project_matches"] and job["workspace_matches"]
    advertised = [
        name
        for name in stack.model_requests()[0]["tools"]
        if name not in _ADVERTISED_PLACEHOLDERS
    ]
    assert advertised == [f"{TOOL_PREFIX}list_files", f"{TOOL_PREFIX}read_file"]


@pytest.mark.parametrize(
    "builtin",
    ["Bash", "Read", "Write", "Edit", "WebFetch", "Task", "DeferredToolPlaceholder"],
)
def test_built_in_tools_cannot_run(stack_factory: Any, builtin: str) -> None:
    stack = stack_factory(
        FAKE_TOOL_CALL=_call(builtin, {"command": "id", "file_path": "/etc/passwd"})
    )
    summary = stack.drive()
    assert summary["jobs"] == []
    advertised = stack.model_requests()[0]["tools"]
    assert all(
        name in {*_ADVERTISED_PLACEHOLDERS} or name.startswith(TOOL_PREFIX)
        for name in advertised
    ), advertised


def test_a_tool_that_was_not_granted_cannot_run(stack_factory: Any) -> None:
    stack = stack_factory(
        FAKE_TOOL_CALL=_call(f"{TOOL_PREFIX}run_command", {"command": "id"})
    )
    summary = stack.drive()
    assert summary["jobs"] == []
    assert f"{TOOL_PREFIX}run_command" not in stack.model_requests()[0]["tools"]


@pytest.mark.parametrize(
    "smuggled",
    [
        {"run_id": str(uuid.uuid4())},
        {"project_id": str(uuid.uuid4())},
        {"workspace_handle": str(uuid.uuid4())},
        {"operation": "run_command"},
    ],
)
def test_model_supplied_identity_never_reaches_a_worker(
    stack_factory: Any, smuggled: dict[str, str]
) -> None:
    stack = stack_factory(
        FAKE_TOOL_CALL=_call(f"{TOOL_PREFIX}read_file", {"path": "a.py", **smuggled})
    )
    summary = stack.drive()
    for job in summary["jobs"]:
        assert job["operation"] == "read_file"
        assert job["run_matches"] and job["project_matches"]
        assert job["workspace_matches"]
        smuggled_keys = set(smuggled) - {"operation"}
        assert not smuggled_keys & set(job["payload"])
        assert job["payload"]["operation"] == "read_file"
        assert not set(smuggled.values()) & {str(v) for v in job["payload"].values()}


def test_the_credential_store_never_reaches_the_model_or_a_worker(
    stack_factory: Any,
) -> None:
    stack = stack_factory(
        FAKE_TOOL_CALL=_call(
            f"{TOOL_PREFIX}read_file",
            {"path": "../../home/claude/.claude/.credentials.json"},
        )
    )
    summary = stack.drive()
    assert SENTINEL not in json.dumps(summary)
    assert summary["jobs"] == []  # a traversal path is invalid, never executed
    assert not any(e.get("sentinel_in_request") for e in stack.model_requests())


def test_the_cli_starts_in_an_empty_directory(stack_factory: Any) -> None:
    summary = stack_factory().drive(operations=["read_file"])
    assert summary["empty_cwd_listing"] == []


def test_a_tool_free_run_advertises_no_tools_to_the_model(stack_factory: Any) -> None:
    stack = stack_factory()
    summary = stack.drive(purpose="background", operations=[])
    assert summary["state"] == "completed", summary
    assert stack.model_requests()[0]["tools"] == []


def test_cli_retries_are_bounded_to_two(stack_factory: Any) -> None:
    stack = stack_factory(FAKE_HTTP_STATUS="500")
    summary = stack.drive(purpose="background", operations=[])
    assert summary["state"] in {"failed", "blocked"}, summary
    assert 1 <= len(stack.model_requests()) <= MAX_MODEL_ATTEMPTS


def test_an_authentication_rejection_requires_login(stack_factory: Any) -> None:
    stack = stack_factory(FAKE_HTTP_STATUS="401")
    summary = stack.drive(purpose="background", operations=[])
    assert summary["state"] == "login_required", summary


def test_a_rate_limit_is_reported_as_quota_exhausted(stack_factory: Any) -> None:
    stack = stack_factory(FAKE_HTTP_STATUS="429")
    summary = stack.drive(purpose="background", operations=[])
    assert summary["state"] == "quota_exhausted", summary
    assert len(stack.model_requests()) <= MAX_MODEL_ATTEMPTS


def test_the_image_has_no_docker_socket_or_project_toolchain(
    service_image: str,
) -> None:
    probe_script = (
        "test ! -e /var/run/docker.sock && echo no-socket; "
        "test ! -e /run/docker.sock && echo no-run-socket; "
        "echo uid=$(id -u); "
        "test -x /opt/claude/claude && echo cli-present; "
        "for tool in git node npm uv make; do "
        "command -v $tool >/dev/null && echo tool-$tool; done; true"
    )
    listing = docker(
        "run", "--rm", "--entrypoint", "sh", service_image, "-c", probe_script
    ).stdout.split()
    assert {"no-socket", "no-run-socket", "uid=10002", "cli-present"} <= set(listing)
    assert not [item for item in listing if item.startswith("tool-")]
