"""Launcher request handling against a fake Docker adapter (flow and limits)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
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
        self.import_stdout = b'{"ok":true,"files":2,"directories":1,"total_bytes":9}'
        self.import_stdin: bytes = b""
        self.volumes: set[str] = set()
        self.removed_volumes: list[str] = []

    async def run_worker(
        self, argv: list[str], stdin: bytes, timeout_seconds: float
    ) -> WorkerRun:
        self.argvs.append(argv)
        self.started.set()
        if argv[-1] == "import":
            self.import_stdin = stdin
            return WorkerRun(returncode=0, stdout=self.import_stdout, timed_out=False)
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

    async def volume_exists(self, name: str) -> bool:
        return name in self.volumes

    async def remove_volume(self, name: str) -> None:
        self.removed_volumes.append(name)
        self.volumes.discard(name)


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


# ---------------------------------------------------------------------------
# Workspace import
# ---------------------------------------------------------------------------

ARCHIVE = b"tar-bytes-never-parsed-by-the-launcher"


def _import_frame(
    token: str, workspace: UUID, project: UUID, size: int, **options: object
) -> bytes:
    return _frame(
        token,
        "import_workspace",
        {
            "workspace_handle": str(workspace),
            "project_id": str(project),
            "archive_bytes": size,
            "options": options,
        },
    )


async def _body(data: bytes, size: int) -> bytes:
    assert size == len(data)
    return data


def _reader(data: bytes = ARCHIVE):  # type: ignore[no-untyped-def]
    async def read(size: int) -> bytes:
        return await _body(data, size)

    return read


async def test_import_runs_the_fixed_profile_worker_and_registers_the_workspace() -> (
    None
):
    docker = FakeDocker()
    launcher = _launcher(docker)
    project, workspace = uuid4(), uuid4()
    response = await launcher.handle_frame(
        _import_frame(ADMIN, workspace, project, len(ARCHIVE)), _reader()
    )
    assert response.ok
    assert response.import_outcome is not None
    assert response.import_outcome.files == 2  # noqa: PLR2004
    argv = docker.argvs[0]
    assert argv[-1] == "import"
    assert argv[argv.index("--network") + 1] == "none"
    assert f"src=roboco_ws_{workspace.hex},dst=/workspace" in " ".join(argv)
    assert docker.import_stdin.endswith(b"\n" + ARCHIVE)
    # now a run can be registered against the imported workspace
    run = uuid4()
    registered = await launcher.handle_frame(
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
    assert registered.ok


async def test_import_requires_the_admin_token() -> None:
    docker = FakeDocker()
    launcher = _launcher(docker)
    response = await launcher.handle_frame(
        _import_frame(EXECUTOR, uuid4(), uuid4(), len(ARCHIVE)), _reader()
    )
    assert response.reason_code == "unauthorized"
    assert not docker.argvs


async def test_import_never_reads_the_body_before_authorization() -> None:
    launcher = _launcher(FakeDocker())

    async def must_not_read(_size: int) -> bytes:
        raise AssertionError("body read before authorization")

    response = await launcher.handle_frame(
        _import_frame("x" * 30, uuid4(), uuid4(), 10), must_not_read
    )
    assert response.reason_code == "unauthorized"


async def test_import_refuses_an_existing_or_registered_workspace() -> None:
    docker = FakeDocker()
    launcher = _launcher(docker)
    project, workspace = uuid4(), uuid4()
    docker.volumes.add(f"roboco_ws_{workspace.hex}")
    refused = await launcher.handle_frame(
        _import_frame(ADMIN, workspace, project, len(ARCHIVE)), _reader()
    )
    assert refused.reason_code == "workspace_exists"
    assert not docker.removed_volumes  # a pre-existing volume is never removed
    assert not docker.argvs


async def test_refused_import_removes_the_volume_and_registers_nothing() -> None:
    docker = FakeDocker()
    docker.import_stdout = (
        b'{"ok":false,"reason_code":"secrets_suspected",'
        b'"findings":[{"path":"a.txt","rule":"anthropic_key"}]}'
    )
    launcher = _launcher(docker)
    project, workspace = uuid4(), uuid4()
    response = await launcher.handle_frame(
        _import_frame(ADMIN, workspace, project, len(ARCHIVE)), _reader()
    )
    assert not response.ok
    assert response.reason_code == "secrets_suspected"
    assert response.import_outcome is not None
    assert response.import_outcome.findings[0].rule == "anthropic_key"
    assert docker.removed_volumes == [f"roboco_ws_{workspace.hex}"]
    run = await launcher.handle_frame(
        _frame(
            ADMIN,
            "register_run",
            {
                "run_id": str(uuid4()),
                "project_id": str(project),
                "workspace_handle": str(workspace),
            },
        )
    )
    assert run.reason_code == "workspace_unknown"


async def test_garbled_worker_output_and_timeout_fail_closed_and_clean_up() -> None:
    docker = FakeDocker()
    docker.import_stdout = b"not json"
    launcher = _launcher(docker)
    workspace = uuid4()
    response = await launcher.handle_frame(
        _import_frame(ADMIN, workspace, uuid4(), len(ARCHIVE)), _reader()
    )
    assert response.reason_code == "import_result_invalid"
    assert docker.removed_volumes == [f"roboco_ws_{workspace.hex}"]


async def test_truncated_archive_body_is_refused_and_cleaned_up() -> None:
    docker = FakeDocker()
    launcher = _launcher(docker)

    async def short(size: int) -> bytes:
        raise asyncio.IncompleteReadError(b"", size)

    workspace = uuid4()
    response = await launcher.handle_frame(
        _import_frame(ADMIN, workspace, uuid4(), 10), short
    )
    assert response.reason_code == "archive_incomplete"
    assert not docker.argvs


async def test_import_frame_without_a_body_channel_is_refused() -> None:
    launcher = _launcher(FakeDocker())
    response = await launcher.handle_frame(_import_frame(ADMIN, uuid4(), uuid4(), 10))
    assert response.reason_code == "archive_body_unavailable"


@pytest.mark.parametrize(
    "bad_options",
    [
        {"image": "evil"},
        {"extra_deny_globs": "not-a-list"},
        {"allowed_findings": [""]},
    ],
)
async def test_import_options_cannot_carry_anything_but_the_two_lists(
    bad_options: dict[str, object],
) -> None:
    docker = FakeDocker()
    response = await _launcher(docker).handle_frame(
        _import_frame(ADMIN, uuid4(), uuid4(), 10, **bad_options), _reader()
    )
    assert response.reason_code == "frame_invalid"
    assert not docker.argvs


async def test_archive_size_is_bounded_by_the_protocol() -> None:
    response = await _launcher(FakeDocker()).handle_frame(
        _import_frame(ADMIN, uuid4(), uuid4(), 10**12), _reader()
    )
    assert response.reason_code == "frame_invalid"
