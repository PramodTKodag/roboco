"""Local review artifacts: a task's work captured as a verified patch.

A developer's changes live only in the offline worker's workspace. Capturing one
asks that worker for the unreviewed changes, re-inspects the returned bytes on
the controller (the worker's own filtering is never trusted), and stores them
with their SHA-256. QA and the owner review exactly those bytes; every later read
re-hashes and re-inspects before serving them. Nothing here talks to a forge.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

from sqlalchemy import select

from roboco.db.tables import ReviewArtifactTable, TaskTable
from roboco.foundation.policy.review_workflow import ArtifactStatus
from roboco.runtime.subscription.patch_inspection import (
    PatchRefused,
    added_documentation_paths,
    inspect_patch,
)
from roboco.runtime.subscription.project_executor import (
    project_executor_provider,
)
from roboco.services.base import BaseService

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from roboco.runtime.subscription.project_executor import (
        ProjectExecutor,
        ProjectExecutorProvider,
    )


class ReviewArtifactRefused(Exception):
    """The artifact cannot be captured or served. Carries a reason code only."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code)


@dataclass(frozen=True)
class CapturedArtifact:
    artifact: ReviewArtifactTable
    created: bool


@dataclass(frozen=True)
class VerifiedPatch:
    """Patch bytes that matched their digest and passed inspection just now."""

    artifact_id: UUID
    task_id: UUID
    sha256: str
    paths: tuple[str, ...]
    content: bytes


def _digest(patch: str) -> str:
    return hashlib.sha256(patch.encode("utf-8")).hexdigest()


class ReviewArtifactService(BaseService):
    """Capture, serve, accept and reject a task's local review artifact."""

    service_name: ClassVar[str] = "review_artifact"

    def __init__(
        self,
        session: AsyncSession,
        executors: ProjectExecutorProvider | None = None,
    ) -> None:
        super().__init__(session)
        self._executors = executors

    def _provider(self) -> ProjectExecutorProvider:
        return self._executors or project_executor_provider()

    async def _executor_for(self, task: TaskTable) -> ProjectExecutor:
        if task.project_id is None:
            raise ReviewArtifactRefused("task_has_no_project")
        return await self._provider().executor_for(
            cast("UUID", task.project_id), cast("UUID", task.id)
        )

    async def current_for(self, task: TaskTable) -> ReviewArtifactTable | None:
        if task.review_artifact_id is None:
            return None
        return await self.session.get(ReviewArtifactTable, task.review_artifact_id)

    async def capture(self, task: TaskTable, *, agent_id: UUID) -> CapturedArtifact:
        """Store the task's unreviewed workspace changes as its open artifact.

        Re-submitting unchanged work returns the existing artifact. New work after
        a bounce supersedes the previous open artifact. A truncated, empty,
        denied-path or secret-bearing patch is refused and nothing is stored.
        Raises ``ProjectExecutionUnavailable`` when the worker cannot answer.
        """
        executor = await self._executor_for(task)
        outcome = await executor.export_patch()
        if outcome.truncated:
            raise ReviewArtifactRefused("patch_truncated")
        return await self._store(task, outcome.patch, agent_id)

    async def _store(
        self, task: TaskTable, patch: str, agent_id: UUID
    ) -> CapturedArtifact:
        """Persist ``patch`` as the task's open artifact (idempotent on equal bytes).

        Every stored byte passed ``inspect_patch`` here, whatever the caller did.
        """
        try:
            report = inspect_patch(patch)
        except PatchRefused as exc:
            self.log.warning(
                "review_artifact_refused",
                task_id=str(task.id),
                reason=exc.reason_code,
            )
            raise ReviewArtifactRefused(exc.reason_code, exc.detail) from exc
        digest = _digest(patch)
        current = await self.current_for(task)
        if (
            current is not None
            and current.status == ArtifactStatus.OPEN
            and current.sha256 == digest
        ):
            return CapturedArtifact(artifact=current, created=False)
        if current is not None and current.status == ArtifactStatus.OPEN:
            current.status = ArtifactStatus.SUPERSEDED
            current.decided_at = datetime.now(UTC)
        artifact = ReviewArtifactTable(
            task_id=task.id,
            captured_by=agent_id,
            patch=patch,
            sha256=digest,
            byte_size=len(patch.encode("utf-8")),
            paths=list(report.paths),
            status=ArtifactStatus.OPEN.value,
        )
        self.session.add(artifact)
        await self.session.flush()
        task.review_artifact_id = cast("Any", artifact.id)
        await self.session.flush()
        self.log.info(
            "review_artifact_captured",
            task_id=str(task.id),
            artifact_id=str(artifact.id),
            files=len(report.paths),
        )
        return CapturedArtifact(artifact=artifact, created=True)

    async def capture_documentation(
        self, task: TaskTable, *, agent_id: UUID
    ) -> CapturedArtifact:
        """Fold the documenter's new files into the artifact QA already reviewed.

        The new artifact supersedes the reviewed one only when every reviewed file
        section is unchanged and the additions are documentation. Anything else is
        refused and the reviewed artifact stays current.
        """
        current = await self._open_artifact(task)
        outcome = await (await self._executor_for(task)).export_patch()
        if outcome.truncated:
            raise ReviewArtifactRefused("patch_truncated")
        if _digest(outcome.patch) == current.sha256:
            return CapturedArtifact(artifact=current, created=False)
        try:
            added = added_documentation_paths(current.patch, outcome.patch)
        except PatchRefused as exc:
            raise ReviewArtifactRefused(exc.reason_code, exc.detail) from exc
        self.log.info(
            "review_artifact_documentation_added",
            task_id=str(task.id),
            files=len(added),
        )
        return await self._store(task, outcome.patch, agent_id)

    async def verified_patch(self, artifact_id: UUID) -> VerifiedPatch:
        """The artifact's bytes, re-hashed and re-inspected; never a stale copy."""
        artifact = await self.session.get(ReviewArtifactTable, artifact_id)
        if artifact is None:
            raise ReviewArtifactRefused("artifact_not_found")
        if _digest(artifact.patch) != artifact.sha256:
            self.log.error("review_artifact_digest_mismatch", id=str(artifact_id))
            raise ReviewArtifactRefused("artifact_digest_mismatch")
        try:
            report = inspect_patch(artifact.patch)
        except PatchRefused as exc:
            raise ReviewArtifactRefused(exc.reason_code, exc.detail) from exc
        return VerifiedPatch(
            artifact_id=artifact.id,
            task_id=artifact.task_id,
            sha256=artifact.sha256,
            paths=report.paths,
            content=artifact.patch.encode("utf-8"),
        )

    async def present_files(self, task: TaskTable) -> list[str]:
        """Files the task's artifact leaves in the tree (deletions excluded).

        Re-reads and re-inspects the stored bytes, so a validator is never pointed
        at paths from an unverified copy. Empty when the task has no artifact.
        """
        artifact = await self.current_for(task)
        if artifact is None:
            return []
        await self.verified_patch(artifact.id)
        return list(inspect_patch(artifact.patch).present_paths)

    async def accept(self, task: TaskTable) -> ReviewArtifactTable:
        """Mark the task's open artifact accepted and advance the workspace baseline.

        The worker is asked first: if it cannot record the new baseline the
        artifact stays open, so the next task never diffs against stale state.
        """
        artifact = await self._open_artifact(task)
        await self.verified_patch(artifact.id)
        executor = await self._executor_for(task)
        await executor.accept_changes()
        artifact.status = ArtifactStatus.ACCEPTED.value
        artifact.decided_at = datetime.now(UTC)
        await self.session.flush()
        return artifact

    async def reject(self, task: TaskTable) -> ReviewArtifactTable:
        """Mark the task's open artifact rejected; the work stays in the workspace."""
        artifact = await self._open_artifact(task)
        artifact.status = ArtifactStatus.REJECTED.value
        artifact.decided_at = datetime.now(UTC)
        await self.session.flush()
        return artifact

    async def _open_artifact(self, task: TaskTable) -> ReviewArtifactTable:
        artifact = await self.current_for(task)
        if artifact is None:
            raise ReviewArtifactRefused("no_artifact")
        if artifact.status != ArtifactStatus.OPEN:
            raise ReviewArtifactRefused("artifact_not_open", artifact.status)
        return artifact

    async def accepted_series_under(self, root_task_id: UUID) -> list[VerifiedPatch]:
        """Every accepted artifact in the task's subtree, in acceptance order.

        Each patch is re-hashed and re-inspected before it is returned, so the
        owner never receives bytes that differ from what was reviewed. Applying
        the series in order reproduces the accepted work on the owner's checkout.
        """
        subtree = [root_task_id]
        frontier = [root_task_id]
        while frontier:
            rows = await self.session.execute(
                select(TaskTable.id).where(TaskTable.parent_task_id.in_(frontier))
            )
            frontier = [task_id for (task_id,) in rows.all()]
            subtree.extend(frontier)
        accepted = await self.accepted_for_tasks(subtree)
        return [await self.verified_patch(artifact.id) for artifact in accepted]

    async def accepted_for_tasks(
        self, task_ids: list[UUID]
    ) -> list[ReviewArtifactTable]:
        """Accepted artifacts of the given tasks, oldest decision first."""
        if not task_ids:
            return []
        rows = await self.session.execute(
            select(ReviewArtifactTable)
            .where(
                ReviewArtifactTable.task_id.in_(task_ids),
                ReviewArtifactTable.status == ArtifactStatus.ACCEPTED.value,
            )
            .order_by(ReviewArtifactTable.decided_at, ReviewArtifactTable.created_at)
        )
        return list(rows.scalars())


def get_review_artifact_service(session: AsyncSession) -> ReviewArtifactService:
    return ReviewArtifactService(session)
