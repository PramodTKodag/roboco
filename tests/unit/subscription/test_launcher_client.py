"""Client talks the real frame protocol to a real Launcher over loopback TCP."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from roboco.runtime.subscription.contracts import WorkerJob, WorkerOperation
from roboco.runtime.subscription.launcher_client import (
    LauncherAdmin,
    LauncherRequestFailed,
    WorkerLauncher,
)
from roboco.runtime.subscription.launcher_policy import (
    ClientAuthenticator,
    LauncherConfig,
    LauncherRegistry,
)
from roboco.runtime.subscription.launcher_server import Launcher
from tests.unit.subscription.test_launcher_server import ADMIN, EXECUTOR, FakeDocker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def endpoint() -> AsyncIterator[tuple[str, int]]:
    launcher = Launcher(
        config=LauncherConfig(image="sha256:" + "c" * 64),
        registry=LauncherRegistry(run_ttl=timedelta(minutes=30)),
        auth=ClientAuthenticator(admin_token=ADMIN, executor_token=EXECUTOR),
        docker=FakeDocker(),
    )
    server = await asyncio.start_server(
        launcher._handle_connection, "127.0.0.1", 0, limit=4 * 1024 * 1024
    )
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield host, port
    finally:
        server.close()
        await server.wait_closed()


def _job(project, workspace, run) -> WorkerJob:  # type: ignore[no-untyped-def]
    return WorkerJob(
        job_id=uuid4(),
        run_id=run,
        project_id=project,
        workspace_handle=workspace,
        operation=WorkerOperation.RUN_COMMAND,
        payload={"operation": "run_command", "command": "true"},  # type: ignore[arg-type]
    )


async def test_register_then_execute_round_trip(endpoint: tuple[str, int]) -> None:
    host, port = endpoint
    admin = LauncherAdmin(host, port, ADMIN)
    launcher = WorkerLauncher(host, port, EXECUTOR)
    project, workspace, run = uuid4(), uuid4(), uuid4()
    await admin.register_workspace(workspace, project)
    await admin.register_run(run, project, workspace)
    result = await launcher.execute(_job(project, workspace, run))
    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert result.reason_code is None


async def test_rejection_is_returned_as_a_structured_result(
    endpoint: tuple[str, int],
) -> None:
    host, port = endpoint
    launcher = WorkerLauncher(host, port, EXECUTOR)
    result = await launcher.execute(_job(uuid4(), uuid4(), uuid4()))
    assert result.exit_code is None
    assert result.reason_code == "run_unknown"


async def test_wrong_token_is_unauthorized(endpoint: tuple[str, int]) -> None:
    host, port = endpoint
    result = await WorkerLauncher(host, port, "w" * 30).execute(
        _job(uuid4(), uuid4(), uuid4())
    )
    assert result.reason_code == "unauthorized"


async def test_unreachable_launcher_blocks_instead_of_hanging() -> None:
    result = await WorkerLauncher("127.0.0.1", 1, EXECUTOR).execute(
        _job(uuid4(), uuid4(), uuid4())
    )
    assert result.reason_code == "launcher_unreachable"
    assert result.exit_code is None


async def test_admin_failures_raise_with_the_reason_code(
    endpoint: tuple[str, int],
) -> None:
    host, port = endpoint
    admin = LauncherAdmin(host, port, ADMIN)
    with pytest.raises(LauncherRequestFailed) as caught:
        await admin.register_run(uuid4(), uuid4(), uuid4())
    assert caught.value.reason_code == "workspace_unknown"
