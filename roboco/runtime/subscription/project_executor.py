"""Controller-side seam for running project commands in offline workers.

Project code (lint, typecheck, build, test, codegen) never runs in a trusted
process. A caller asks a ``ProjectExecutor`` to run a command; the executor sends
one ``run_command`` job to the fixed-profile launcher and returns the exit status
and output. Anything that is not a real exit status (unreachable launcher,
cancelled or expired run, malformed result) raises ``ProjectExecutionUnavailable``
so it can never be mistaken for a passing or skipped command.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from pydantic import ValidationError

from roboco.runtime.subscription.contracts import (
    COMMAND_TIMEOUT_SECONDS,
    MAX_CONVENTION_FILES,
    AcceptChangesPayload,
    CheckConventionsPayload,
    ExportPatchPayload,
    RunCommandPayload,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

COMMAND_TIMED_OUT_EXIT_CODE = 124
_TIMEOUT_REASON = "command_timeout"


class ProjectExecutionUnavailable(Exception):
    """Project code could not be run. Carries a stable reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class CommandOutcome:
    """A command that really ran: its exit status and combined output."""

    exit_code: int
    output: str
    truncated: bool


@dataclass(frozen=True)
class ConventionsOutcome:
    """Validator findings (JSON lines), or the reason it could not run."""

    finding_lines: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def could_not_run(self) -> bool:
        return self.reason is not None


@dataclass(frozen=True)
class PatchOutcome:
    """The workspace's unreviewed changes as patch text; ``truncated`` means cut off."""

    patch: str
    truncated: bool


class WorkerExecutionClient(Protocol):
    async def execute(self, job: WorkerJob) -> WorkerResult: ...


class CommandRunner(Protocol):
    async def run_command(self, command: str) -> CommandOutcome:
        """Run one command in the project's offline workspace."""
        ...


class ConventionsChecker(Protocol):
    async def check_conventions(self, files: list[str]) -> ConventionsOutcome:
        """Validate ``files`` against the project's conventions in the worker."""
        ...


class PatchWorkspace(Protocol):
    async def export_patch(self) -> PatchOutcome:
        """Export the workspace's changes since its baseline, minus denied paths."""
        ...

    async def accept_changes(self) -> None:
        """Make the workspace's current state the baseline for the next export."""
        ...


class ProjectExecutor(CommandRunner, ConventionsChecker, PatchWorkspace, Protocol):
    """Everything the controller may ask of a project's offline workspace."""


class ProjectExecutorProvider(Protocol):
    async def executor_for(self, project_id: UUID, task_id: UUID) -> ProjectExecutor:
        """The executor bound to the workspace holding ``task_id``'s work."""
        ...


class UnconfiguredProjectExecutorProvider:
    """Default provider: no worker is configured, so nothing may run."""

    async def executor_for(self, project_id: UUID, task_id: UUID) -> ProjectExecutor:
        logger.error(
            "project execution refused: no offline worker is configured",
            extra={"project_id": str(project_id), "task_id": str(task_id)},
        )
        raise ProjectExecutionUnavailable("worker_not_configured")


class WorkerProjectExecutor:
    """Runs commands as jobs in one registered workspace through the launcher."""

    def __init__(
        self,
        launcher: WorkerExecutionClient,
        *,
        run_id: UUID,
        project_id: UUID,
        workspace_handle: UUID,
    ) -> None:
        self._launcher = launcher
        self.run_id = run_id
        self.project_id = project_id
        self.workspace_handle = workspace_handle

    async def run_command(self, command: str) -> CommandOutcome:
        job = self._job_for(command)
        result = await self._launcher.execute(job)
        return _outcome_of(job, result)

    async def check_conventions(self, files: list[str]) -> ConventionsOutcome:
        lines: list[str] = []
        for start in range(0, len(files), MAX_CONVENTION_FILES):
            outcome = await self._check_conventions_batch(
                files[start : start + MAX_CONVENTION_FILES]
            )
            if outcome.could_not_run:
                return outcome
            lines.extend(outcome.finding_lines)
        return ConventionsOutcome(finding_lines=tuple(lines))

    async def export_patch(self) -> PatchOutcome:
        job = self._job(WorkerOperation.EXPORT_PATCH, ExportPatchPayload)
        result = await self._launcher.execute(job)
        _require_answer_to(job, result)
        if _exit_code_of(result) != 0:
            raise ProjectExecutionUnavailable("patch_export_failed")
        return PatchOutcome(patch=result.stdout, truncated=result.truncated)

    async def accept_changes(self) -> None:
        job = self._job(WorkerOperation.ACCEPT_CHANGES, AcceptChangesPayload)
        result = await self._launcher.execute(job)
        _require_answer_to(job, result)
        if _exit_code_of(result) != 0:
            raise ProjectExecutionUnavailable("accept_changes_failed")

    async def _check_conventions_batch(self, files: list[str]) -> ConventionsOutcome:
        job = self._job(
            WorkerOperation.CHECK_CONVENTIONS, CheckConventionsPayload, files=files
        )
        result = await self._launcher.execute(job)
        _require_answer_to(job, result)
        exit_code = _exit_code_of(result)
        if exit_code == 0 and not result.truncated:
            lines = tuple(ln for ln in result.stdout.splitlines() if ln.strip())
            return ConventionsOutcome(finding_lines=lines)
        reason = (
            "findings exceeded the result size limit"
            if result.truncated and exit_code == 0
            else (result.stderr.strip() or "validator could not run")
        )
        return ConventionsOutcome(reason=reason[:300])

    def _job_for(self, command: str) -> WorkerJob:
        return self._job(
            WorkerOperation.RUN_COMMAND, RunCommandPayload, command=command
        )

    def _job(
        self,
        operation: WorkerOperation,
        payload_type: type[RunCommandPayload]
        | type[CheckConventionsPayload]
        | type[ExportPatchPayload]
        | type[AcceptChangesPayload],
        **payload_fields: object,
    ) -> WorkerJob:
        try:
            return WorkerJob(
                job_id=uuid4(),
                run_id=self.run_id,
                project_id=self.project_id,
                workspace_handle=self.workspace_handle,
                operation=operation,
                payload=payload_type(**payload_fields),  # type: ignore[arg-type]
            )
        except ValidationError as exc:
            raise ProjectExecutionUnavailable("command_rejected") from exc


def _require_answer_to(job: WorkerJob, result: WorkerResult) -> None:
    if result.job_id != job.job_id:
        logger.error("worker answered a different job than the one sent")
        raise ProjectExecutionUnavailable("worker_result_mismatch")


def _exit_code_of(result: WorkerResult) -> int:
    """The real exit status, or raise: a missing one is never a pass."""
    if result.reason_code is not None:
        raise ProjectExecutionUnavailable(result.reason_code)
    if result.exit_code is None:
        raise ProjectExecutionUnavailable("worker_result_invalid")
    return result.exit_code


def _outcome_of(job: WorkerJob, result: WorkerResult) -> CommandOutcome:
    _require_answer_to(job, result)
    if result.reason_code == _TIMEOUT_REASON and result.exit_code is None:
        return CommandOutcome(
            exit_code=COMMAND_TIMED_OUT_EXIT_CODE,
            output=f"command timed out after {COMMAND_TIMEOUT_SECONDS}s",
            truncated=False,
        )
    return CommandOutcome(
        exit_code=_exit_code_of(result),
        output=result.stdout + result.stderr,
        truncated=result.truncated,
    )


_provider: ProjectExecutorProvider = UnconfiguredProjectExecutorProvider()


def configure_project_executor_provider(provider: ProjectExecutorProvider) -> None:
    """Install the process-wide provider (composition root and tests only)."""
    global _provider  # noqa: PLW0603
    _provider = provider


def project_executor_provider() -> ProjectExecutorProvider:
    return _provider
