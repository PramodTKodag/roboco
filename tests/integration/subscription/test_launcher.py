"""Real-Docker proof of the fixed worker profile (container-level evidence).

Drives the real launcher against the local Docker engine. Only disposable
resources are created (see docker_support). Skipped with an explicit reason when
Docker is unavailable.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from roboco.runtime.subscription.launcher_policy import VOLUME_PREFIX, WORKER_LABEL

from tests.integration.subscription.docker_support import (
    ADMIN,
    docker,
    docker_unavailable_reason,
    frame,
    make_launcher,
    new_session,
    owned_container_ids,
    wait_for_container,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        docker_unavailable_reason() is not None,
        reason=docker_unavailable_reason() or "docker available",
    ),
]


async def test_command_runs_as_unprivileged_user_in_a_persistent_workspace(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_session(worker_image, created_volumes)
    first = await session.run_command("id -u; echo kept > /workspace/f.txt")
    assert first.ok, first.reason_code
    assert first.result.stdout.strip() == "10001"
    second = await session.run_command("cat /workspace/f.txt")
    assert second.result.stdout.strip() == "kept"


async def test_worker_has_no_network_no_credentials_and_a_read_only_root(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_session(worker_image, created_volumes)
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
    session = await new_session(worker_image, created_volumes)
    running = asyncio.create_task(session.run_command("sleep 8"))
    container = await wait_for_container(session.run)
    inspected = json.loads(docker("inspect", container).stdout)[0]
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
    one = await new_session(worker_image, created_volumes)
    two = await new_session(worker_image, created_volumes)
    await one.run_command("echo secret-one > /workspace/s.txt")
    seen = await two.run_command("ls -A /workspace")
    assert seen.result.stdout.strip() == ""


async def test_hard_timeout_kills_the_container(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_session(worker_image, created_volumes, hard_timeout=3)
    response = await session.run_command("sleep 60")
    assert (response.ok, response.reason_code) == (False, "worker_timeout")
    await asyncio.sleep(1)
    assert owned_container_ids(session.run) == []


async def test_cancel_removes_the_running_container(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_session(worker_image, created_volumes)
    running = asyncio.create_task(session.run_command("sleep 60"))
    await wait_for_container(session.run)
    cancelled = await session.launcher.handle_frame(
        frame(ADMIN, "cancel_run", {"run_id": str(session.run)})
    )
    assert cancelled.ok
    response = await asyncio.wait_for(running, 30)
    assert (response.ok, response.reason_code) == (False, "run_cancelled")
    await asyncio.sleep(1)
    assert owned_container_ids(session.run) == []


async def test_reconcile_kills_only_labelled_workers(worker_image: str) -> None:
    launcher = make_launcher(worker_image)
    labelled = docker(
        "run", "--detach", "--rm", "--network", "none",
        "--label", f"{WORKER_LABEL}=1", "--label", "roboco.personal.test=1",
        "--entrypoint", "sleep", worker_image, "60",
    ).stdout.strip()  # fmt: skip
    bystander = docker(
        "run", "--detach", "--rm", "--network", "none",
        "--label", "roboco.personal.test=1",
        "--entrypoint", "sleep", worker_image, "60",
    ).stdout.strip()  # fmt: skip
    try:
        assert labelled in await launcher.reconcile_orphans(dry_run=True)
        assert labelled in owned_container_ids()
        killed = await launcher.reconcile_orphans(dry_run=False)
        assert labelled in killed
        assert bystander not in killed
        await asyncio.sleep(1)
        assert labelled not in owned_container_ids()
        assert bystander in docker("ps", "--quiet", "--no-trunc").stdout.split()
    finally:
        docker("rm", "--force", labelled, bystander, check=False)
