"""Hostile project commands run through the executor seam stay in the sandbox.

Container-level evidence: the real ``WorkerProjectExecutor`` drives the real
fixed-profile worker through the launcher. The secrets are synthetic, planted in
the controller's environment and on the host filesystem only to prove they never
appear in any result the project can influence.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from roboco.runtime.subscription.contracts import WorkerJob, WorkerResult
from roboco.runtime.subscription.project_executor import (
    ProjectExecutionUnavailable,
    WorkerProjectExecutor,
)
from roboco.services.gateway.quality_gate import run_quality_commands

from tests.integration.subscription.docker_support import (
    Session,
    docker_unavailable_reason,
    new_imported_session,
    tar_of,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        docker_unavailable_reason() is not None,
        reason=docker_unavailable_reason() or "docker available",
    ),
]

SYNTHETIC_ENV_SECRET = "synthetic-controller-secret-7f3a91"
SYNTHETIC_FILE_SECRET = "synthetic-host-file-secret-b2c4e8"
LEAK_MARKERS = (SYNTHETIC_ENV_SECRET, SYNTHETIC_FILE_SECRET)

HOSTILE_PROJECT = {
    "Makefile": (
        b"quality:\n\t@env | sort\n\t@cat /proc/1/environ | tr '\\0' '\\n'\n\t@exit 3\n"
    ),
    "build_backend.py": (
        b"import os, socket, subprocess\n"
        b"print('ENV', sorted(os.environ))\n"
        b"subprocess.Popen(['sleep', '600'])\n"
        b"s = socket.socket()\n"
        b"s.settimeout(3)\n"
        b"try:\n"
        b"    s.connect(('host.docker.internal', 80))\n"
        b"    print('NETWORK-REACHED-HOST')\n"
        b"except OSError as exc:\n"
        b"    print('NETWORK-BLOCKED', type(exc).__name__)\n"
    ),
}


class _LauncherAdapter:
    """Presents the launcher's frame protocol as the executor's client port."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, job: WorkerJob) -> WorkerResult:
        response = await self._session.execute(job)
        if response.ok:
            return response.result  # type: ignore[no-any-return]
        return WorkerResult(
            job_id=job.job_id,
            exit_code=None,
            stdout="",
            stderr="",
            truncated=False,
            reason_code=response.reason_code,
        )


def _executor_for(session: Session) -> WorkerProjectExecutor:
    return WorkerProjectExecutor(
        _LauncherAdapter(session),
        run_id=session.run,
        project_id=session.project,
        workspace_handle=session.workspace,
    )


@pytest.fixture
def planted_secrets() -> Iterator[Path]:
    """Synthetic secrets in the controller env and on the host filesystem."""
    host_dir = Path(tempfile.mkdtemp(prefix="roboco-host-secret-"))
    secret_file = host_dir / "credentials.json"
    secret_file.write_text(SYNTHETIC_FILE_SECRET)
    os.environ["SYNTHETIC_CONTROLLER_TOKEN"] = SYNTHETIC_ENV_SECRET
    try:
        yield host_dir
    finally:
        os.environ.pop("SYNTHETIC_CONTROLLER_TOKEN", None)
        secret_file.unlink()
        host_dir.rmdir()


async def test_hostile_project_commands_never_see_controller_secrets(
    worker_image: str, created_volumes: list[str], planted_secrets: Path
) -> None:
    session = await new_imported_session(
        worker_image, created_volumes, tar_of(HOSTILE_PROJECT)
    )
    executor = _executor_for(session)
    commands = [
        "make quality",
        "python3 build_backend.py",
        "env",
        f"cat {planted_secrets}/credentials.json; "
        "cat ~/.claude/.credentials.json ~/.config/gh/hosts.yml ~/.ssh/id_rsa",
        "for p in /proc/[0-9]*; do tr '\\0' '\\n' < $p/environ; done",
        "ls /var/run/docker.sock /run/docker.sock /Users /host",
    ]
    for command in commands:
        outcome = await executor.run_command(command)
        for marker in LEAK_MARKERS:
            assert marker not in outcome.output, command
    quality = await executor.run_command("make quality")
    assert quality.exit_code != 0


async def test_hostile_build_backend_cannot_reach_the_network(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(
        worker_image, created_volumes, tar_of(HOSTILE_PROJECT)
    )
    outcome = await _executor_for(session).run_command("python3 build_backend.py")
    assert outcome.exit_code == 0
    assert "NETWORK-BLOCKED" in outcome.output
    assert "NETWORK-REACHED-HOST" not in outcome.output


async def test_project_hooks_and_children_are_contained_in_the_worker(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(
        worker_image, created_volumes, tar_of(HOSTILE_PROJECT)
    )
    executor = _executor_for(session)
    await executor.run_command("python3 build_backend.py")
    after = await executor.run_command("ps -e -o comm= | sort | uniq -c")
    # Each job runs in its own disposable container: the spawned child is gone.
    assert "sleep" not in after.output


async def test_missing_toolchain_fails_the_gate_with_an_actionable_hint(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(
        worker_image, created_volumes, tar_of(HOSTILE_PROJECT)
    )
    result = await run_quality_commands(
        _executor_for(session), [("lint", "definitely-not-installed --check")]
    )
    assert not result.passed
    assert not result.skipped
    assert "pre-fetched" in result.output


async def test_unavailable_worker_blocks_instead_of_passing_or_skipping(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(
        worker_image, created_volumes, tar_of(HOSTILE_PROJECT)
    )
    executor = WorkerProjectExecutor(
        _LauncherAdapter(session),
        run_id=UUID(int=1),  # a run the launcher never registered
        project_id=session.project,
        workspace_handle=session.workspace,
    )
    with pytest.raises(ProjectExecutionUnavailable):
        await run_quality_commands(executor, [("lint", "true")])


async def test_check_conventions_runs_in_the_built_worker_image(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(
        worker_image, created_volumes, tar_of({"app.py": b"x = 1\n"})
    )
    outcome = await _executor_for(session).check_conventions(["app.py"])
    assert not outcome.could_not_run, outcome.reason
    assert outcome.finding_lines == ()


async def test_unparseable_conventions_file_could_not_run_never_a_clean_pass(
    worker_image: str, created_volumes: list[str]
) -> None:
    project = {
        "app.py": b"x = 1\n",
        ".roboco/conventions.yml": b"version: [unterminated\n",
    }
    session = await new_imported_session(worker_image, created_volumes, tar_of(project))
    outcome = await _executor_for(session).check_conventions(["app.py"])
    assert outcome.could_not_run
    assert outcome.finding_lines == ()
