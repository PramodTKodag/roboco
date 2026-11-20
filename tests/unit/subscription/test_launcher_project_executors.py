"""The launcher-backed provider registers a run per project and fails closed."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from roboco.runtime.subscription.launcher_client import LauncherRequestFailed
from roboco.runtime.subscription.launcher_project_executors import (
    RUN_REUSE_SECONDS,
    LauncherProjectExecutorProvider,
)
from roboco.runtime.subscription.project_executor import ProjectExecutionUnavailable

if TYPE_CHECKING:
    from roboco.runtime.subscription.contracts import WorkerJob, WorkerResult

_EXPECTED_RUNS_AFTER_REUSE_WINDOW = 2


class FakeRegistration:
    def __init__(self, failure: Exception | None = None) -> None:
        self.workspaces: list[tuple[UUID, UUID]] = []
        self.runs: list[tuple[UUID, UUID, UUID]] = []
        self.failure = failure

    async def register_workspace(
        self, workspace_handle: UUID, project_id: UUID
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.workspaces.append((workspace_handle, project_id))

    async def register_run(
        self, run_id: UUID, project_id: UUID, workspace_handle: UUID
    ) -> None:
        self.runs.append((run_id, project_id, workspace_handle))


class FakeLauncher:
    async def execute(self, job: WorkerJob) -> WorkerResult:
        raise AssertionError(f"no job expected: {job.operation}")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _provider(
    registration: FakeRegistration, handle: UUID | None, clock: Clock
) -> LauncherProjectExecutorProvider:
    async def workspace_of(project_id: UUID) -> UUID | None:
        del project_id
        return handle

    return LauncherProjectExecutorProvider(
        registration, FakeLauncher(), workspace_of, clock=clock
    )


async def test_registers_the_workspace_then_one_run_bound_to_the_executor() -> None:
    project, handle = uuid4(), uuid4()
    registration = FakeRegistration()
    provider = _provider(registration, handle, Clock())

    executor = await provider.executor_for(project, uuid4())

    assert registration.workspaces == [(handle, project)]
    assert len(registration.runs) == 1
    run_id, run_project, run_handle = registration.runs[0]
    assert (run_project, run_handle) == (project, handle)
    assert (executor.run_id, executor.project_id) == (run_id, project)  # type: ignore[attr-defined]


async def test_reuses_the_run_until_it_is_close_to_expiry() -> None:
    project = uuid4()
    registration = FakeRegistration()
    clock = Clock()
    provider = _provider(registration, uuid4(), clock)

    first = await provider.executor_for(project, uuid4())
    clock.now += RUN_REUSE_SECONDS - 1
    assert await provider.executor_for(project, uuid4()) is first
    clock.now += 2
    assert await provider.executor_for(project, uuid4()) is not first
    assert len(registration.runs) == _EXPECTED_RUNS_AFTER_REUSE_WINDOW


async def test_a_project_without_a_workspace_is_refused() -> None:
    provider = _provider(FakeRegistration(), None, Clock())

    with pytest.raises(ProjectExecutionUnavailable) as refused:
        await provider.executor_for(uuid4(), uuid4())

    assert refused.value.reason_code == "workspace_not_linked"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            LauncherRequestFailed("workspace_owned_by_other_project"),
            "workspace_owned_by_other_project",
        ),
        (ConnectionRefusedError(), "launcher_unreachable"),
        (TimeoutError(), "launcher_unreachable"),
    ],
)
async def test_a_failed_registration_never_yields_an_executor(
    failure: Exception, reason: str
) -> None:
    provider = _provider(FakeRegistration(failure), uuid4(), Clock())

    with pytest.raises(ProjectExecutionUnavailable) as refused:
        await provider.executor_for(uuid4(), uuid4())

    assert refused.value.reason_code == reason
