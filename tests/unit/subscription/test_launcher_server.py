"""Launcher request handling against a fake Docker adapter (flow and limits)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

from roboco.runtime.subscription.contracts import WorkerResult
from roboco.runtime.subscription.launcher_policy import (
    ClientAuthenticator,
    LauncherConfig,
    LauncherRegistry,
)
from roboco.runtime.subscription.launcher_server import Launcher, WorkerRun

ADMIN = "admin-token-synthetic-0123456789"
EXECUTOR = "executor-token-synthetic-0123456789"
CONFIG = LauncherConfig(image="sha256:" + "b" * 64)


class FakeDocker:
    def __init__(self, *, stdout: bytes | None = None, hang: bool = False) -> None:
        self.stdout = stdout
        self.hang = hang
        self.argvs: list[list[str]] = []
        self.killed: list[str] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def run_worker(
        self, argv: list[str], stdin: bytes, timeout_seconds: float
    ) -> WorkerRun:
        self.argvs.append(argv)
        self.started.set()
        if self.hang:
            try:
                await asyncio.wait_for(self.release.wait(), timeout_seconds)
            except TimeoutError:
                return WorkerRun(returncode=None, stdout=b"", timed_out=True)
        job = json.loads(stdin)
        out = self.stdout
        if out is None:
            out = (
                WorkerResult(
                    job_id=UUID(job["job_id"]),
                    exit_code=0,
                    stdout="ok",
                    stderr="",
                    truncated=False,
                    reason_code=None,
                )
                .model_dump_json()
                .encode()
            )
        return WorkerRun(returncode=0, stdout=out, timed_out=False)

    async def kill_containers(self, names: list[str]) -> None:
        self.killed.extend(names)

    async def list_owned_containers(self, run_id: UUID | None = None) -> list[str]:
        del run_id
        return []


def _launcher(
    docker: FakeDocker,
    *,
    max_concurrent: int = 2,
    max_pending: int = 16,
    hard_timeout_seconds: float = 30,
) -> Launcher:
    return Launcher(
        config=CONFIG,
        registry=LauncherRegistry(run_ttl=timedelta(minutes=30)),
        auth=ClientAuthenticator(admin_token=ADMIN, executor_token=EXECUTOR),
        docker=docker,
        max_concurrent=max_concurrent,
        max_pending=max_pending,
        hard_timeout_seconds=hard_timeout_seconds,
    )


def _frame(token: str, action: str, body: dict[str, object]) -> bytes:
    return json.dumps({"token": token, "action": action, "body": body}).encode()


async def _register(launcher: Launcher) -> tuple[UUID, UUID, UUID]:
    project, workspace, run = uuid4(), uuid4(), uuid4()
    ok = await launcher.handle_frame(
        _frame(
            ADMIN,
            "register_workspace",
            {"workspace_handle": str(workspace), "project_id": str(project)},
        )
    )
    assert ok.ok
    ok = await launcher.handle_frame(
        _frame(
            ADMIN,
            "register_run",
            {
                "run_id": str(run),
                "project_id": str(project),
                "workspace_handle": str(workspace),
            },
        )
    )
    assert ok.ok
    return project, workspace, run


def _execute(project: UUID, workspace: UUID, run: UUID) -> bytes:
    job = {
        "job_id": str(uuid4()),
        "run_id": str(run),
        "project_id": str(project),
        "workspace_handle": str(workspace),
        "operation": "run_command",
        "payload": {"operation": "run_command", "command": "true"},
    }
    return _frame(EXECUTOR, "execute", {"job": job})


async def test_registered_job_executes_and_returns_the_worker_result() -> None:
    docker = FakeDocker()
    launcher = _launcher(docker)
    response = await launcher.handle_frame(_execute(*await _register(launcher)))
    assert response.ok
    assert response.result is not None
    assert response.result.stdout == "ok"
    assert len(docker.argvs) == 1


async def test_bad_token_and_wrong_role_are_refused_before_any_docker_call() -> None:
    docker = FakeDocker()
    launcher = _launcher(docker)
    project, workspace, run = await _register(launcher)
    bad = await launcher.handle_frame(
        _frame(
            "x" * 30,
            "register_run",
            {
                "run_id": str(run),
                "project_id": str(project),
                "workspace_handle": str(workspace),
            },
        )
    )
    assert (bad.ok, bad.reason_code) == (False, "unauthorized")
    # An executor token cannot register runs; an admin token cannot execute.
    wrong = await launcher.handle_frame(
        _frame(
            EXECUTOR,
            "register_workspace",
            {"workspace_handle": str(uuid4()), "project_id": str(uuid4())},
        )
    )
    assert (wrong.ok, wrong.reason_code) == (False, "unauthorized")
    admin_exec = await launcher.handle_frame(
        _execute(project, workspace, run).replace(EXECUTOR.encode(), ADMIN.encode())
    )
    assert (admin_exec.ok, admin_exec.reason_code) == (False, "unauthorized")
    assert docker.argvs == []


async def test_malformed_and_launch_shaped_frames_are_refused() -> None:
    docker = FakeDocker()
    launcher = _launcher(docker)
    response = await launcher.handle_frame(b"garbage")
    assert (response.ok, response.reason_code) == (False, "frame_not_json")
    response = await launcher.handle_frame(
        _frame(EXECUTOR, "execute", {"job": {}, "image": "evil"})
    )
    assert (response.ok, response.reason_code) == (False, "frame_invalid")
    assert docker.argvs == []


async def test_unregistered_run_is_refused_without_docker() -> None:
    docker = FakeDocker()
    launcher = _launcher(docker)
    project, workspace, _ = await _register(launcher)
    response = await launcher.handle_frame(_execute(project, workspace, uuid4()))
    assert (response.ok, response.reason_code) == (False, "run_unknown")
    assert docker.argvs == []


async def test_worker_output_that_is_not_a_matching_result_is_a_failure() -> None:
    for stdout in (b"not json", b"{}", b""):
        docker = FakeDocker(stdout=stdout)
        launcher = _launcher(docker)
        response = await launcher.handle_frame(_execute(*await _register(launcher)))
        assert (response.ok, response.reason_code) == (False, "worker_result_invalid")
    other = (
        WorkerResult(
            job_id=uuid4(),
            exit_code=0,
            stdout="",
            stderr="",
            truncated=False,
            reason_code=None,
        )
        .model_dump_json()
        .encode()
    )
    docker = FakeDocker(stdout=other)
    launcher = _launcher(docker)
    response = await launcher.handle_frame(_execute(*await _register(launcher)))
    assert (response.ok, response.reason_code) == (False, "worker_result_invalid")


async def test_worker_timeout_is_reported_and_the_container_is_killed() -> None:
    docker = FakeDocker(hang=True)
    launcher = _launcher(docker, hard_timeout_seconds=0.2)
    response = await launcher.handle_frame(_execute(*await _register(launcher)))
    assert (response.ok, response.reason_code) == (False, "worker_timeout")
    assert len(docker.killed) == 1


async def test_cancel_kills_the_inflight_worker_and_blocks_new_jobs() -> None:
    docker = FakeDocker(hang=True)
    launcher = _launcher(docker, hard_timeout_seconds=30)
    project, workspace, run = await _register(launcher)
    running = asyncio.create_task(
        launcher.handle_frame(_execute(project, workspace, run))
    )
    await asyncio.wait_for(docker.started.wait(), 2)
    cancelled = await launcher.handle_frame(
        _frame(ADMIN, "cancel_run", {"run_id": str(run)})
    )
    assert cancelled.ok
    assert len(docker.killed) == 1
    docker.release.set()
    await running
    again = await launcher.handle_frame(_execute(project, workspace, run))
    assert (again.ok, again.reason_code) == (False, "run_cancelled")


async def test_queue_is_bounded() -> None:
    docker = FakeDocker(hang=True)
    launcher = _launcher(
        docker, max_concurrent=1, max_pending=1, hard_timeout_seconds=30
    )
    project, workspace, run = await _register(launcher)
    first = asyncio.create_task(
        launcher.handle_frame(_execute(project, workspace, run))
    )
    await asyncio.wait_for(docker.started.wait(), 2)
    second = asyncio.create_task(
        launcher.handle_frame(_execute(project, workspace, run))
    )
    await asyncio.sleep(0.05)
    third = await launcher.handle_frame(_execute(project, workspace, run))
    assert (third.ok, third.reason_code) == (False, "queue_full")
    docker.release.set()
    await asyncio.gather(first, second)
