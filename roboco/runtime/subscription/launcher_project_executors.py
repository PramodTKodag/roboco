"""Project executors backed by the worker launcher.

The controller asks this provider for the executor of a project's offline
workspace. Each executor is bound to a freshly registered launcher run, so a job
can only name a workspace the controller registered for that project. Anything
that cannot be registered raises ``ProjectExecutionUnavailable``: project code is
never run anywhere else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from roboco.runtime.subscription.launcher_client import LauncherRequestFailed
from roboco.runtime.subscription.project_executor import (
    ProjectExecutionUnavailable,
    ProjectExecutor,
    WorkerExecutionClient,
    WorkerProjectExecutor,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

logger = logging.getLogger(__name__)

# A registered run expires on the launcher after 45 minutes; reuse stops well
# before that so an executor handed out never carries an expired run.
RUN_REUSE_SECONDS = 20 * 60


class RunRegistration(Protocol):
    async def register_workspace(
        self, workspace_handle: UUID, project_id: UUID
    ) -> None: ...

    async def register_run(
        self, run_id: UUID, project_id: UUID, workspace_handle: UUID
    ) -> None: ...


class WorkspaceLookup(Protocol):
    async def __call__(self, project_id: UUID) -> UUID | None: ...


@dataclass(frozen=True)
class _Registered:
    executor: WorkerProjectExecutor
    workspace_handle: UUID
    registered_at: float


class LauncherProjectExecutorProvider:
    """One registered launcher run per project, reused for a bounded time."""

    def __init__(
        self,
        registration: RunRegistration,
        launcher: WorkerExecutionClient,
        workspace_of: WorkspaceLookup,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registration = registration
        self._launcher = launcher
        self._workspace_of = workspace_of
        self._clock = clock
        self._registered: dict[UUID, _Registered] = {}
        self._lock = asyncio.Lock()

    async def executor_for(self, project_id: UUID, task_id: UUID) -> ProjectExecutor:
        handle = await self._workspace_of(project_id)
        if handle is None:
            logger.warning(
                "project execution refused: no offline workspace is linked",
                extra={"project_id": str(project_id), "task_id": str(task_id)},
            )
            raise ProjectExecutionUnavailable("workspace_not_linked")
        async with self._lock:
            current = self._registered.get(project_id)
            if (
                current is not None
                and current.workspace_handle == handle
                and self._clock() - current.registered_at < RUN_REUSE_SECONDS
            ):
                return current.executor
            registered = await self._register(project_id, handle)
            self._registered[project_id] = registered
            return registered.executor

    async def _register(self, project_id: UUID, handle: UUID) -> _Registered:
        run_id = uuid4()
        try:
            await self._registration.register_workspace(handle, project_id)
            await self._registration.register_run(run_id, project_id, handle)
        except (LauncherRequestFailed, OSError, TimeoutError) as exc:
            reason = getattr(exc, "reason_code", "launcher_unreachable")
            logger.warning(
                "project execution refused: run registration failed",
                extra={"project_id": str(project_id), "reason": reason},
            )
            raise ProjectExecutionUnavailable(reason) from exc
        return _Registered(
            executor=WorkerProjectExecutor(
                self._launcher,
                run_id=run_id,
                project_id=project_id,
                workspace_handle=handle,
            ),
            workspace_handle=handle,
            registered_at=self._clock(),
        )
