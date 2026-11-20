"""Shared real-Docker helpers for the subscription integration and security suites.

Only disposable resources are created: containers labelled
``roboco.personal.worker=1`` and volumes named ``roboco_ws_*`` that a test
registered itself. Unrelated containers and volumes are never touched.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import tarfile
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from roboco.runtime.subscription.contracts import (
    ImportOptions,
    ImportOutcome,
    WorkerJob,
    WorkerOperation,
)
from roboco.runtime.subscription.launcher_policy import (
    VOLUME_PREFIX,
    WORKER_LABEL,
    ClientAuthenticator,
    LauncherConfig,
    LauncherRegistry,
)
from roboco.runtime.subscription.launcher_server import DockerCli, Launcher

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO_ROOT = Path(__file__).parents[3]
IMAGE_TAG = "roboco-personal-worker:itest"
ADMIN = "admin-token-itest-0123456789abcdef"
EXECUTOR = "executor-token-itest-0123456789abcdef"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=300,
    )


def docker_unavailable_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not installed"
    if docker("info", check=False).returncode != 0:
        return "docker engine not reachable"
    return None


def make_launcher(image: str, hard_timeout: float = 60) -> Launcher:
    return Launcher(
        config=LauncherConfig(image=image),
        registry=LauncherRegistry(run_ttl=timedelta(minutes=10)),
        auth=ClientAuthenticator(admin_token=ADMIN, executor_token=EXECUTOR),
        docker=DockerCli(),
        hard_timeout_seconds=hard_timeout,
    )


def frame(token: str, action: str, body: dict[str, Any]) -> bytes:
    return json.dumps({"token": token, "action": action, "body": body}).encode()


def tar_of(entries: Mapping[str, bytes | tarfile.TarInfo]) -> bytes:
    """A plain tar: bytes values become regular files, TarInfo values go as-is."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, value in entries.items():
            if isinstance(value, tarfile.TarInfo):
                value.name = name
                archive.addfile(value)
                continue
            info = tarfile.TarInfo(name)
            info.size = len(value)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


class Session:
    """One project workspace and run against a launcher."""

    def __init__(self, launcher: Launcher, volumes: list[str]) -> None:
        self.launcher = launcher
        self.project = uuid4()
        self.workspace = uuid4()
        self.run = uuid4()
        self.volume = f"{VOLUME_PREFIX}{self.workspace.hex}"
        volumes.append(self.volume)

    async def _admin(self, action: str, body: dict[str, Any]) -> None:
        response = await self.launcher.handle_frame(frame(ADMIN, action, body))
        assert response.ok, response.reason_code

    async def register(self) -> None:
        await self._admin(
            "register_workspace",
            {"workspace_handle": str(self.workspace), "project_id": str(self.project)},
        )
        await self.register_run()

    async def register_run(self) -> None:
        await self._admin(
            "register_run",
            {
                "run_id": str(self.run),
                "project_id": str(self.project),
                "workspace_handle": str(self.workspace),
            },
        )

    async def import_archive(
        self, archive: bytes, options: ImportOptions | None = None
    ) -> tuple[bool, ImportOutcome | None, str | None]:
        """Run the real import; returns (ok, outcome, reason_code)."""
        body = {
            "workspace_handle": str(self.workspace),
            "project_id": str(self.project),
            "archive_bytes": len(archive),
            "options": (options or ImportOptions()).model_dump(mode="json"),
        }

        async def read_body(size: int) -> bytes:
            assert size == len(archive)
            return archive

        response = await self.launcher.handle_frame(
            frame(ADMIN, "import_workspace", body), read_body
        )
        return response.ok, response.import_outcome, response.reason_code

    def job(self, operation: WorkerOperation, payload: dict[str, Any]) -> WorkerJob:
        return WorkerJob(
            job_id=uuid4(),
            run_id=self.run,
            project_id=self.project,
            workspace_handle=self.workspace,
            operation=operation,
            payload={"operation": operation.value, **payload},  # type: ignore[arg-type]
        )

    async def execute(self, job: WorkerJob) -> Any:
        return await self.launcher.handle_frame(
            frame(EXECUTOR, "execute", {"job": job.model_dump(mode="json")})
        )

    async def run_command(self, command: str) -> Any:
        return await self.execute(
            self.job(WorkerOperation.RUN_COMMAND, {"command": command})
        )

    def volume_exists(self) -> bool:
        return bool(
            docker(
                "volume", "ls", "--quiet", "--filter", f"name={self.volume}"
            ).stdout.strip()
        )


async def new_session(
    image: str, volumes: list[str], hard_timeout: float = 60
) -> Session:
    session = Session(make_launcher(image, hard_timeout), volumes)
    await session.register()
    return session


async def new_imported_session(
    image: str,
    volumes: list[str],
    archive: bytes,
    options: ImportOptions | None = None,
) -> Session:
    session = Session(make_launcher(image), volumes)
    ok, outcome, reason = await session.import_archive(archive, options)
    assert ok, (reason, outcome)
    await session.register_run()
    return session


def owned_container_ids(run: UUID | None = None) -> list[str]:
    args = ["ps", "--quiet", "--no-trunc", "--filter", f"label={WORKER_LABEL}=1"]
    if run is not None:
        args += ["--filter", f"label=roboco.personal.run={run}"]
    return docker(*args).stdout.split()


async def wait_for_container(run: UUID) -> str:
    for _ in range(100):
        ids = owned_container_ids(run)
        if ids:
            return ids[0]
        await asyncio.sleep(0.1)
    raise AssertionError("worker container never started")


def new_session_for(image: str, volumes: list[str]) -> Session:
    """A session with no registration yet (for import-first flows)."""
    return Session(make_launcher(image), volumes)
