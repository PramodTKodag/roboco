"""Real-Docker proof of the fixed worker profile (container-level evidence).

Builds the worker image and drives the real launcher against the local Docker
engine. Only disposable resources are created: containers labelled
``roboco.personal.worker=1`` and volumes named ``roboco_ws_*`` that this module
registered itself. Unrelated containers and volumes are never listed for
removal. Skipped with an explicit reason when Docker is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from roboco.runtime.subscription.contracts import WorkerJob, WorkerOperation
from roboco.runtime.subscription.launcher_policy import (
    VOLUME_PREFIX,
    WORKER_LABEL,
    ClientAuthenticator,
    LauncherConfig,
    LauncherRegistry,
)
from roboco.runtime.subscription.launcher_server import DockerCli, Launcher

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).parents[3]
_IMAGE_TAG = "roboco-personal-worker:itest"
ADMIN = "admin-token-itest-0123456789abcdef"
EXECUTOR = "executor-token-itest-0123456789abcdef"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=300,
    )


def _docker_unavailable_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not installed"
    if _docker("info", check=False).returncode != 0:
        return "docker engine not reachable"
    return None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _docker_unavailable_reason() is not None,
        reason=_docker_unavailable_reason() or "docker available",
    ),
]


@pytest.fixture(scope="module")
def worker_image() -> str:
    _docker(
        "build",
        "--quiet",
        "--file",
        str(_REPO_ROOT / "docker" / "task-worker.Dockerfile"),
        "--tag",
        _IMAGE_TAG,
        str(_REPO_ROOT),
    )
    return _IMAGE_TAG


@pytest.fixture
def created_volumes() -> Iterator[list[str]]:
    volumes: list[str] = []
    yield volumes
    for name in volumes:
        assert name.startswith(VOLUME_PREFIX)
        _docker("volume", "rm", "--force", name, check=False)


def _make_launcher(image: str, hard_timeout: float = 60) -> Launcher:
    return Launcher(
        config=LauncherConfig(image=image),
        registry=LauncherRegistry(run_ttl=timedelta(minutes=10)),
        auth=ClientAuthenticator(admin_token=ADMIN, executor_token=EXECUTOR),
        docker=DockerCli(),
        hard_timeout_seconds=hard_timeout,
    )


def _frame(token: str, action: str, body: dict[str, Any]) -> bytes:
    return json.dumps({"token": token, "action": action, "body": body}).encode()


class Session:
    """One registered project workspace and run against a launcher."""

    def __init__(self, launcher: Launcher, volumes: list[str]) -> None:
        self.launcher = launcher
        self.project = uuid4()
        self.workspace = uuid4()
        self.run = uuid4()
        volumes.append(f"{VOLUME_PREFIX}{self.workspace.hex}")

    async def register(self) -> None:
        for action, body in (
            (
                "register_workspace",
                {
                    "workspace_handle": str(self.workspace),
                    "project_id": str(self.project),
                },
            ),
            (
                "register_run",
                {
                    "run_id": str(self.run),
                    "project_id": str(self.project),
                    "workspace_handle": str(self.workspace),
                },
            ),
        ):
            response = await self.launcher.handle_frame(_frame(ADMIN, action, body))
            assert response.ok, response.reason_code

    def job(self, command: str) -> WorkerJob:
        return WorkerJob(
            job_id=uuid4(),
            run_id=self.run,
            project_id=self.project,
            workspace_handle=self.workspace,
            operation=WorkerOperation.RUN_COMMAND,
            payload={"operation": "run_command", "command": command},  # type: ignore[arg-type]
        )

    async def run_command(self, command: str) -> Any:
        frame = _frame(
            EXECUTOR, "execute", {"job": self.job(command).model_dump(mode="json")}
        )
        return await self.launcher.handle_frame(frame)


async def _session(image: str, volumes: list[str], hard_timeout: float = 60) -> Session:
    session = Session(_make_launcher(image, hard_timeout), volumes)
    await session.register()
    return session


def _owned_container_ids(run: UUID | None = None) -> list[str]:
    args = ["ps", "--quiet", "--no-trunc", "--filter", f"label={WORKER_LABEL}=1"]
    if run is not None:
        args += ["--filter", f"label=roboco.personal.run={run}"]
    return _docker(*args).stdout.split()


async def _wait_for_container(run: UUID) -> str:
    for _ in range(100):
        ids = _owned_container_ids(run)
        if ids:
            return ids[0]
        await asyncio.sleep(0.1)
    raise AssertionError("worker container never started")


async def test_command_runs_as_unprivileged_user_in_a_persistent_workspace(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await _session(worker_image, created_volumes)
    first = await session.run_command("id -u; echo kept > /workspace/f.txt")
    assert first.ok, first.reason_code
    assert first.result.stdout.strip() == "10001"
    second = await session.run_command("cat /workspace/f.txt")
    assert second.result.stdout.strip() == "kept"


async def test_worker_has_no_network_no_credentials_and_a_read_only_root(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await _session(worker_image, created_volumes)
    probe = (
        'python -c "import socket;s=socket.socket();s.settimeout(2);'
        "s.connect(('1.1.1.1',443))\" 2>&1 | tail -1; "
        "touch /etc/should-fail 2>&1 | tail -1; "
        "echo tmp-ok > /tmp/t && cat /tmp/t; "
        "ls /var/run/docker.sock 2>&1 | tail -1; "
        "env | cut -d= -f1 | sort | tr '\\n' ' '"
    )
    response = await session.run_command(probe)
    assert response.ok, response.reason_code
    out = response.result.stdout
    assert "Network is unreachable" in out or "OSError" in out
    assert "Read-only file system" in out
    assert "tmp-ok" in out
    assert "No such file" in out
    for forbidden in ("ANTHROPIC", "CLAUDE", "TOKEN", "KEY", "SECRET"):
        assert forbidden not in out.split("No such file")[-1]


async def test_running_container_has_the_exact_fixed_security_profile(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await _session(worker_image, created_volumes)
    running = asyncio.create_task(session.run_command("sleep 8"))
    container = await _wait_for_container(session.run)
    inspected = json.loads(_docker("inspect", container).stdout)[0]
    host = inspected["HostConfig"]
    assert host["NetworkMode"] == "none"
    assert host["IpcMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert not host["CapAdd"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert host["PidsLimit"] == 256  # noqa: PLR2004
    assert host["Memory"] == 2 * 1024**3
    assert host["MemorySwap"] == 2 * 1024**3
    assert host["NanoCpus"] == 2 * 10**9
    assert not host["Binds"]
    assert host["PidMode"] in ("", "private")
    assert inspected["Config"]["User"] == "10001:10001"
    mounts = inspected["Mounts"]
    assert [(m["Type"], m["Destination"]) for m in mounts] == [("volume", "/workspace")]
    assert mounts[0]["Name"] == f"{VOLUME_PREFIX}{session.workspace.hex}"
    assert not any("docker.sock" in m.get("Source", "") for m in mounts)
    assert set(inspected["Config"]["Labels"]) >= {WORKER_LABEL}
    await running


async def test_workspaces_are_isolated_from_each_other(
    worker_image: str, created_volumes: list[str]
) -> None:
    one = await _session(worker_image, created_volumes)
    two = await _session(worker_image, created_volumes)
    await one.run_command("echo secret-one > /workspace/s.txt")
    seen = await two.run_command("ls -A /workspace")
    assert seen.result.stdout.strip() == ""


async def test_hard_timeout_kills_the_container(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await _session(worker_image, created_volumes, hard_timeout=3)
    response = await session.run_command("sleep 60")
    assert (response.ok, response.reason_code) == (False, "worker_timeout")
    await asyncio.sleep(1)
    assert _owned_container_ids(session.run) == []


async def test_cancel_removes_the_running_container(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await _session(worker_image, created_volumes)
    running = asyncio.create_task(session.run_command("sleep 60"))
    await _wait_for_container(session.run)
    cancelled = await session.launcher.handle_frame(
        _frame(ADMIN, "cancel_run", {"run_id": str(session.run)})
    )
    assert cancelled.ok
    response = await asyncio.wait_for(running, 30)
    assert (response.ok, response.reason_code) == (False, "run_cancelled")
    await asyncio.sleep(1)
    assert _owned_container_ids(session.run) == []


async def test_reconcile_kills_only_labelled_workers(worker_image: str) -> None:
    launcher = _make_launcher(worker_image)
    labelled = _docker(
        "run", "--detach", "--rm", "--network", "none",
        "--label", f"{WORKER_LABEL}=1", "--label", "roboco.personal.test=1",
        "--entrypoint", "sleep", worker_image, "60",
    ).stdout.strip()  # fmt: skip
    bystander = _docker(
        "run", "--detach", "--rm", "--network", "none",
        "--label", "roboco.personal.test=1",
        "--entrypoint", "sleep", worker_image, "60",
    ).stdout.strip()  # fmt: skip
    try:
        assert labelled in await launcher.reconcile_orphans(dry_run=True)
        assert labelled in _owned_container_ids()
        killed = await launcher.reconcile_orphans(dry_run=False)
        assert labelled in killed
        assert bystander not in killed
        await asyncio.sleep(1)
        assert labelled not in _owned_container_ids()
        assert bystander in _docker("ps", "--quiet", "--no-trunc").stdout.split()
    finally:
        _docker("rm", "--force", labelled, bystander, check=False)
