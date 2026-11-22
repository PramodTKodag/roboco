"""Doubles shared by the local review integration suites.

Local review never touches a forge: the only boundaries its workflow must not
cross are a git double that fails the test on any forge or host-clone call, and
a project executor that stands in for the offline worker (mocked layer; the
worker itself is covered by the container suites).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from roboco.runtime.subscription.project_executor import (
    PatchOutcome,
    ProjectExecutionUnavailable,
)
from roboco.services.gateway.quality_gate import GateResult
from roboco.services.review_artifact import ReviewArtifactService

if TYPE_CHECKING:
    from uuid import UUID

    from roboco.db.tables import ReviewArtifactTable, TaskTable
    from sqlalchemy.ext.asyncio import AsyncSession

SYNTHETIC_PATCH = (
    "diff --git a/app/health.py b/app/health.py\n"
    "new file mode 100644\n"
    "index 0000000..1111111\n"
    "--- /dev/null\n"
    "+++ b/app/health.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def healthz() -> dict[str, str]:\n"
    '+    return {"status": "ok"}\n'
)


def patch_for(path: str, line: str) -> str:
    """A one-line new-file patch for ``path``."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{line}\n"
    )


class ForgeTouched(AssertionError):
    """The workflow reached a forge or host-clone operation."""


class StrictGit:
    """Only the worker-backed quality gate is reachable; anything else fails."""

    def __init__(self) -> None:
        self.quality_gate_runs = 0
        self.quality_gate_passes = True

    async def run_pre_submit_quality_gate(self, task: Any) -> GateResult:
        del task
        self.quality_gate_runs += 1
        return GateResult(passed=self.quality_gate_passes, skipped=False)

    def __getattr__(self, name: str) -> Any:
        raise ForgeTouched(f"git.{name} was called in the local review workflow")


@dataclass
class FakeWorkspace:
    """The offline worker as the controller sees it."""

    patch: str = SYNTHETIC_PATCH
    truncated: bool = False
    accepted: int = 0
    unavailable: bool = False
    exports: int = 0

    async def export_patch(self) -> PatchOutcome:
        if self.unavailable:
            raise ProjectExecutionUnavailable("worker_not_configured")
        self.exports += 1
        return PatchOutcome(patch=self.patch, truncated=self.truncated)

    async def accept_changes(self) -> None:
        self.accepted += 1

    async def run_command(self, command: str) -> Any:
        raise AssertionError(f"unexpected command {command}")

    async def check_conventions(self, files: list[str]) -> Any:
        raise AssertionError(f"unexpected conventions run for {files}")


class FakeExecutors:
    def __init__(self, workspace: FakeWorkspace) -> None:
        self.workspace = workspace

    async def executor_for(self, project_id: UUID, task_id: UUID) -> FakeWorkspace:
        del project_id, task_id
        return self.workspace


@dataclass
class LocalReviewKit:
    """The local review boundary doubles plus the real artifact service over them."""

    git: StrictGit
    workspace: FakeWorkspace
    artifacts: ReviewArtifactService

    @classmethod
    def create(cls, session: AsyncSession) -> LocalReviewKit:
        workspace = FakeWorkspace()
        return cls(
            git=StrictGit(),
            workspace=workspace,
            artifacts=ReviewArtifactService(session, FakeExecutors(workspace)),
        )

    async def capture_open_artifact(
        self, task: TaskTable, *, agent_id: UUID
    ) -> ReviewArtifactTable:
        """Capture the workspace's current patch as the task's open artifact."""
        captured = await self.artifacts.capture(task, agent_id=agent_id)
        return captured.artifact
