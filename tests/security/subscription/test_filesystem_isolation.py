"""Hostile project code inside a real worker cannot reach anything but its workspace.

Container-level evidence: each test starts the real fixed-profile worker through
the launcher. Secrets here are synthetic and exist only to prove absence.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from roboco.runtime.subscription.contracts import WorkerOperation

from tests.integration.subscription.docker_support import (
    docker_unavailable_reason,
    new_imported_session,
    tar_of,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        docker_unavailable_reason() is not None,
        reason=docker_unavailable_reason() or "docker available",
    ),
]

SENTINEL = b"sibling-workspace-sentinel-bytes"
# Names set by the pinned base image and Dockerfile; GPG_KEY is the public
# fingerprint python.org publishes for release verification, not a secret.
BASE_IMAGE_ENV_NAMES = {
    "GPG_KEY",
    "HOME",
    "HOSTNAME",
    "LANG",
    "PATH",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    "PIP_NO_CACHE_DIR",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "PYTHON_SHA256",
    "PYTHON_VERSION",
}
PROJECT = {"main.py": b"print('hi')\n"}


async def test_sibling_workspace_and_host_paths_are_unreachable(
    worker_image: str, created_volumes: list[str]
) -> None:
    sibling = await new_imported_session(
        worker_image, created_volumes, tar_of({"sentinel.txt": SENTINEL})
    )
    attacker = await new_imported_session(
        worker_image, created_volumes, tar_of(PROJECT)
    )
    host_marker = Path(tempfile.mkdtemp(prefix="roboco-host-marker-"))
    (host_marker / "host-secret.txt").write_bytes(b"synthetic-host-secret")
    try:
        probe = (
            "find / -xdev -name sentinel.txt 2>/dev/null | head -3; "
            f"grep -rls {SENTINEL.decode()} /workspace /tmp /opt /var /home /mnt "
            "2>/dev/null | head -3; "
            f"ls {sibling.volume} /var/lib/docker /Users /host 2>&1 | head -3; "
            f"ls {host_marker} 2>&1 | head -1; "
            "echo END"
        )
        response = await attacker.run_command(probe)
        assert response.ok, response.reason_code
        out = response.result.stdout
        assert SENTINEL.decode() not in out
        assert "sentinel.txt" not in out
        assert "host-secret" not in out
        assert out.rstrip().endswith("END")
    finally:
        (host_marker / "host-secret.txt").unlink()
        host_marker.rmdir()


async def test_worker_has_no_capabilities_control_socket_or_extra_mounts(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(worker_image, created_volumes, tar_of(PROJECT))
    probe = (
        "grep CapEff /proc/self/status; "
        "ls /var/run/docker.sock /run/docker.sock 2>&1 | wc -l; "
        "awk '{print $2}' /proc/mounts | grep -E '^/(workspace|tmp)$' "
        "| sort | tr '\\n' ' '; "
        "echo; id -u; "
        "(touch /etc/x 2>&1; touch /usr/x 2>&1) | wc -l"
    )
    response = await session.run_command(probe)
    assert response.ok, response.reason_code
    lines = response.result.stdout.strip().splitlines()
    assert lines[0].split()[-1] == "0000000000000000"
    assert "/tmp" in lines[2]
    assert "/workspace" in lines[2]
    assert lines[3] == "10001"
    assert lines[4] == "2"  # both writes refused


async def test_process_environment_holds_no_credentials(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(worker_image, created_volumes, tar_of(PROJECT))
    response = await session.run_command(
        "tr '\\0' '\\n' < /proc/1/environ | cut -d= -f1 | sort | tr '\\n' ' '; echo; "
        "ls -A /tmp /root ~ 2>/dev/null | head -5"
    )
    assert response.ok, response.reason_code
    names = set(response.result.stdout.split("\n", 1)[0].split())
    assert names <= BASE_IMAGE_ENV_NAMES, names - BASE_IMAGE_ENV_NAMES


async def test_project_planted_links_are_refused_by_file_operations(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(worker_image, created_volumes, tar_of(PROJECT))
    plant = await session.run_command(
        "ln -s /etc/passwd /workspace/link.txt; "
        "ln -s /etc /workspace/etc_dir; "
        "mkdir /workspace/real && ln /workspace/main.py /workspace/real/hard.py"
    )
    assert plant.ok, plant.reason_code
    expectations = {
        WorkerOperation.READ_FILE: ({"path": "link.txt"}, "symlink_refused"),
    }
    for operation, (payload, reason) in expectations.items():
        refused = await session.execute(session.job(operation, payload))
        assert refused.ok
        assert refused.result.reason_code == reason

    through_dir = await session.execute(
        session.job(WorkerOperation.READ_FILE, {"path": "etc_dir/passwd"})
    )
    assert through_dir.result.reason_code == "symlink_refused"
    write_through = await session.execute(
        session.job(
            WorkerOperation.WRITE_FILE,
            {"path": "etc_dir/evil", "content": "x"},
        )
    )
    assert write_through.result.reason_code == "symlink_refused"
    hard = await session.execute(
        session.job(WorkerOperation.READ_FILE, {"path": "real/hard.py"})
    )
    assert hard.result.reason_code == "hardlink_refused"
    denied = await session.execute(
        session.job(WorkerOperation.READ_FILE, {"path": ".git/config"})
    )
    assert denied.result.reason_code == "path_denied"


async def test_exported_patch_cannot_smuggle_denied_or_credential_paths(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(worker_image, created_volumes, tar_of(PROJECT))
    plant = await session.run_command(
        "echo SYNTHETIC=1 > /workspace/.env; "
        "echo changed >> /workspace/main.py; "
        "mkdir -p /workspace/.ssh && echo k > /workspace/.ssh/id_rsa"
    )
    assert plant.ok, plant.reason_code
    exported = await session.execute(session.job(WorkerOperation.EXPORT_PATCH, {}))
    assert exported.ok, exported.reason_code
    patch = exported.result.stdout
    assert "main.py" in patch
    assert ".env" not in patch
    assert "id_rsa" not in patch
