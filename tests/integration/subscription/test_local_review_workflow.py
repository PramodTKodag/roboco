"""Offline task-to-review flow: no forge, no fake pull request.

Drives a real Choreographer and TaskService over the disposable database. The
only doubles are the two boundaries this workflow must not cross: a git double
that fails the test on any forge or host-clone call, and a project executor that
stands in for the offline worker (mocked layer; the worker itself is covered by
the container suites).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from roboco.db.tables import (
    AgentTable,
    ProjectTable,
    ReviewArtifactTable,
    TaskTable,
)
from roboco.models.base import (
    AgentRole,
    AgentStatus,
    TaskNature,
    TaskStatus,
    TaskType,
    Team,
)
from roboco.runtime.subscription.project_executor import PatchOutcome
from roboco.services.gateway.choreographer import Choreographer, ChoreographerDeps
from roboco.services.gateway.quality_gate import GateResult
from roboco.services.review_artifact import (
    ReviewArtifactRefused,
    ReviewArtifactService,
)
from roboco.services.task import TaskService

from tests.integration.conftest import cleanup_project_durable_rows
from tests.integration.test_full_lifecycle_real_db import (
    _GOOD_PLAN,
    _GOOD_RISKS,
    _GOOD_TC,
    _STEPS,
    _mock_evidence_repo,
    _mock_journal_with_reflect,
    _mock_work_session,
    _seed_main_pm,
    _seed_root_task,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    exports: int = field(default=0)

    async def export_patch(self) -> PatchOutcome:
        from roboco.runtime.subscription.project_executor import (
            ProjectExecutionUnavailable,
        )

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
class LocalReviewHarness:
    c: Choreographer
    task_service: TaskService
    artifacts: ReviewArtifactService
    git: StrictGit
    workspace: FakeWorkspace
    task: TaskTable
    dev: AgentTable
    qa: AgentTable
    doc: AgentTable
    cell_pm: AgentTable


def _agent(role: AgentRole, slug: str) -> AgentTable:
    return AgentTable(
        id=uuid4(),
        name=slug,
        slug=f"{slug}-{uuid4().hex[:8]}",
        role=role,
        team=Team.BACKEND,
        status=AgentStatus.ACTIVE,
        model_config={},
        system_prompt=slug,
        capabilities=[],
        permissions={},
        metrics={},
    )


@pytest_asyncio.fixture
async def harness(
    db_session: AsyncSession, _test_database_url: str
) -> AsyncIterator[LocalReviewHarness]:
    system = AgentTable(
        id=uuid4(),
        name="System",
        slug=f"system-{uuid4().hex[:8]}",
        role=AgentRole.SYSTEM,
        team=None,
        status=AgentStatus.ACTIVE,
        model_config={},
        system_prompt="system",
        capabilities=[],
        permissions={},
        metrics={},
    )
    db_session.add(system)
    await db_session.flush()
    project = ProjectTable(
        id=uuid4(),
        name="Local Review Project",
        slug=f"local-{uuid4().hex[:8]}",
        git_url="https://example.invalid/local-review.git",
        default_branch="main",
        protected_branches=["main"],
        assigned_cell=Team.BACKEND,
        created_by=system.id,
        is_active=True,
        offline_workspace_handle=uuid4(),
    )
    db_session.add(project)
    await db_session.flush()
    dev = _agent(AgentRole.DEVELOPER, "be-dev")
    qa = _agent(AgentRole.QA, "be-qa")
    doc = _agent(AgentRole.DOCUMENTER, "be-doc")
    cell_pm = _agent(AgentRole.CELL_PM, "be-pm")
    db_session.add_all([dev, qa, doc, cell_pm])
    await db_session.flush()
    task = TaskTable(
        id=uuid4(),
        title="Add /healthz endpoint",
        description="Return 200 OK from /healthz",
        status=TaskStatus.PENDING,
        priority=2,
        task_type=TaskType.CODE,
        nature=TaskNature.TECHNICAL,
        team=Team.BACKEND,
        project_id=project.id,
        created_by=system.id,
        assigned_to=dev.id,
        acceptance_criteria=["Returns 200", "Includes status"],
        acceptance_criteria_status=[
            {"criterion": "Returns 200", "referencing_artifact_id": "stub"},
            {"criterion": "Includes status", "referencing_artifact_id": "stub"},
        ],
    )
    db_session.add(task)
    await db_session.flush()

    project_id = project.id
    agent_ids = [system.id, dev.id, qa.id, doc.id, cell_pm.id]
    workspace = FakeWorkspace()
    git = StrictGit()
    task_service = TaskService(db_session)
    artifacts = ReviewArtifactService(db_session, FakeExecutors(workspace))
    deps = ChoreographerDeps(
        task=task_service,
        work_session=_mock_work_session(),
        git=git,
        a2a=AsyncMock(),
        journal=_mock_journal_with_reflect(),
        audit=AsyncMock(),
        evidence_repo=_mock_evidence_repo(),
        review_artifacts=artifacts,
    )
    try:
        yield LocalReviewHarness(
            c=Choreographer(deps),
            task_service=task_service,
            artifacts=artifacts,
            git=git,
            workspace=workspace,
            task=task,
            dev=dev,
            qa=qa,
            doc=doc,
            cell_pm=cell_pm,
        )
    finally:
        await db_session.rollback()
        await cleanup_project_durable_rows(_test_database_url, project_id, agent_ids)


async def _claim_and_work(h: LocalReviewHarness) -> None:
    env = await h.c.i_will_work_on(
        agent_id=h.dev.id,
        task_id=h.task.id,
        plan=_GOOD_PLAN,
        steps=_STEPS,
        technical_considerations=_GOOD_TC,
        risks=_GOOD_RISKS,
    )
    assert env.error is None, env.message
    await h.task_service.add_progress(h.task.id, h.dev.id, "implemented /healthz")
    await h.task_service.record_section_note(
        h.task.id,
        "developer",
        {"summary": "Implemented /healthz and added a test for the happy path."},
    )


async def _submit_to_qa(h: LocalReviewHarness) -> None:
    await _claim_and_work(h)
    env = await h.c.open_pr(h.dev.id, h.task.id)
    assert env.error is None, env.message
    env = await h.c.i_am_done(h.dev.id, h.task.id, "tests pass; route works")
    assert env.error is None, env.message
    assert env.status == "awaiting_qa"


async def _qa_pass(h: LocalReviewHarness) -> None:
    env = await h.c.claim_review(h.qa.id, h.task.id)
    assert env.error is None, env.message
    env = await h.c.pass_review(
        h.qa.id,
        h.task.id,
        notes=(
            "Reviewed the local patch artifact end to end: the endpoint returns 200"
            " with a status field, and both acceptance criteria are met."
        ),
        ac_verdicts=[f"verified: {c}" for c in h.task.acceptance_criteria],
        criteria_verified=[
            {"criterion": c, "evidence": f"verified in the patch: {c}"}
            for c in h.task.acceptance_criteria
        ],
    )
    assert env.error is None, env.message


async def test_task_reaches_owner_review_without_touching_a_forge(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    await _submit_to_qa(h)

    task = await h.task_service.get(h.task.id)
    assert task is not None
    assert task.review_workflow == "local_review"
    assert task.review_artifact_id is not None
    assert task.pr_number is None
    assert task.pr_url is None
    assert task.branch_name is None
    artifact = await h.artifacts.current_for(task)
    assert artifact is not None
    assert artifact.status == "open"
    assert artifact.paths == ["app/health.py"]

    await _qa_pass(h)
    env = await h.c.claim_doc_task(h.doc.id, h.task.id)
    assert env.error is None, env.message
    env = await h.c.i_documented(
        h.doc.id, h.task.id, notes="Documented /healthz in docs/api/health.md",
        files=["docs/api/health.md"],
    )
    assert env.error is None, env.message
    assert env.status == "awaiting_pm_review"

    env = await h.c.complete(
        h.cell_pm.id, h.task.id, notes="Local patch reviewed by QA; accepting it."
    )
    assert env.error is None, env.message
    assert env.status == "completed"

    task = await h.task_service.get(h.task.id)
    assert task is not None
    artifact = await h.artifacts.current_for(task)
    assert artifact is not None
    assert artifact.status == "accepted"
    assert h.workspace.accepted == 1
    assert h.git.quality_gate_runs >= 1

    exported = await h.artifacts.verified_patch(artifact.id)
    assert exported.content == SYNTHETIC_PATCH.encode()
    assert exported.paths == ("app/health.py",)


def _patch_for(path: str, line: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{line}\n"
    )


async def _task_row(h: LocalReviewHarness) -> TaskTable:
    task = await h.task_service.get(h.task.id)
    assert task is not None
    return task


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        (_patch_for("config/.env", "MODE=dev"), "patch_path_denied"),
        (
            _patch_for("app/keys.py", "-----BEGIN PRIVATE KEY-----"),
            "secrets_suspected",
        ),
        ("", "patch_empty"),
    ],
)
async def test_unsafe_changes_are_never_captured(
    harness: LocalReviewHarness, patch: str, reason: str
) -> None:
    h = harness
    h.workspace.patch = patch
    await _claim_and_work(h)

    env = await h.c.open_pr(h.dev.id, h.task.id)

    assert env.error == "invalid_state"
    assert reason in str(env.message)
    assert (await _task_row(h)).review_artifact_id is None
    env = await h.c.i_am_done(h.dev.id, h.task.id, "tests pass; route works")
    assert env.error is not None
    assert (await _task_row(h)).status != TaskStatus.AWAITING_QA


async def test_truncated_export_is_refused_not_stored(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    h.workspace.truncated = True
    await _claim_and_work(h)

    env = await h.c.open_pr(h.dev.id, h.task.id)

    assert env.error == "invalid_state"
    assert "patch_truncated" in str(env.message)
    assert (await _task_row(h)).review_artifact_id is None


async def test_unavailable_worker_blocks_capture_and_submit(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    h.workspace.unavailable = True
    await _claim_and_work(h)

    assert (await h.c.open_pr(h.dev.id, h.task.id)).error == "invalid_state"
    assert (await h.c.i_am_done(h.dev.id, h.task.id, "done")).error is not None
    assert (await _task_row(h)).status != TaskStatus.AWAITING_QA


async def test_failing_quality_checks_block_submit(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    h.git.quality_gate_passes = False
    await _claim_and_work(h)
    assert (await h.c.open_pr(h.dev.id, h.task.id)).error is None

    env = await h.c.i_am_done(h.dev.id, h.task.id, "tests pass; route works")

    assert env.error is not None
    assert (await _task_row(h)).status != TaskStatus.AWAITING_QA


async def test_repeated_capture_keeps_one_artifact(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    await _claim_and_work(h)
    assert (await h.c.open_pr(h.dev.id, h.task.id)).error is None
    first = (await _task_row(h)).review_artifact_id

    assert (await h.c.open_pr(h.dev.id, h.task.id)).error is None

    assert (await _task_row(h)).review_artifact_id == first


async def test_qa_failure_then_new_work_supersedes_the_artifact(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    await _submit_to_qa(h)
    first = (await _task_row(h)).review_artifact_id
    assert (await h.c.claim_review(h.qa.id, h.task.id)).error is None

    env = await h.c.fail_review(
        h.qa.id,
        h.task.id,
        findings=[
            {
                "file": "app/health.py",
                "line": 1,
                "severity": "major",
                "expected": "a typed response model so clients get a stable schema",
                "actual": "returns a bare dict with no declared response shape",
            }
        ],
    )
    assert env.error is None, env.message
    assert (await _task_row(h)).status == TaskStatus.NEEDS_REVISION

    h.workspace.patch = _patch_for("app/health.py", "def healthz() -> Health: ...")
    env = await h.c.i_will_work_on(
        agent_id=h.dev.id,
        task_id=h.task.id,
        plan=_GOOD_PLAN,
        steps=_STEPS,
        technical_considerations=_GOOD_TC,
        risks=_GOOD_RISKS,
    )
    assert env.error is None, env.message
    assert (await h.c.open_pr(h.dev.id, h.task.id)).error is None

    task = await _task_row(h)
    assert task.review_artifact_id != first
    old = await h.artifacts.session.get(ReviewArtifactTable, first)
    assert old is not None
    assert old.status == "superseded"


async def test_documentation_cannot_alter_the_reviewed_change(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    await _submit_to_qa(h)
    await _qa_pass(h)
    assert (await h.c.claim_doc_task(h.doc.id, h.task.id)).error is None
    h.workspace.patch = _patch_for("app/health.py", "return tampered")

    env = await h.c.i_documented(
        h.doc.id,
        h.task.id,
        notes="Documented /healthz in docs/api/health.md",
        files=["docs/api/health.md"],
    )

    assert env.error == "invalid_state"
    assert "documentation_altered_reviewed_change" in str(env.message)
    assert (await _task_row(h)).status == TaskStatus.AWAITING_DOCUMENTATION


async def _seed_cell_and_root(h: LocalReviewHarness) -> tuple[TaskTable, TaskTable]:
    """Parent the harness task under a cell task under a Main-PM root."""
    session = h.task_service.session
    main_pm_id = await _seed_main_pm(session)
    root = await _seed_root_task(
        session,
        project_id=h.task.project_id,
        creator_id=h.task.created_by,
        main_pm_id=main_pm_id,
    )
    root.branch_name = None
    cell = TaskTable(
        id=uuid4(),
        title="Backend cell: healthz",
        description="Cell scope assembling the /healthz work",
        status=TaskStatus.IN_PROGRESS,
        priority=2,
        task_type=TaskType.PLANNING,
        nature=TaskNature.TECHNICAL,
        team=Team.BACKEND,
        project_id=h.task.project_id,
        created_by=h.task.created_by,
        assigned_to=h.cell_pm.id,
        parent_task_id=root.id,
        acceptance_criteria=["The /healthz endpoint ships"],
    )
    session.add(cell)
    await session.flush()
    h.task.parent_task_id = cell.id
    await session.flush()
    return cell, root


async def test_cell_and_root_reach_owner_approval_without_a_pull_request(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    cell, root = await _seed_cell_and_root(h)
    await _submit_to_qa(h)
    await _qa_pass(h)
    assert (await h.c.claim_doc_task(h.doc.id, h.task.id)).error is None
    env = await h.c.i_documented(
        h.doc.id,
        h.task.id,
        notes="Documented /healthz in docs/api/health.md",
        files=["docs/api/health.md"],
    )
    assert env.error is None, env.message
    env = await h.c.complete(h.cell_pm.id, h.task.id, notes="Leaf patch accepted.")
    assert env.error is None, env.message

    env = await h.c.submit_up(
        h.cell_pm.id,
        cell.id,
        notes="Cell scope assembled: /healthz is implemented, reviewed and documented.",
    )
    assert env.error is None, env.message
    assert env.status == "awaiting_pm_review"
    env = await h.c.complete(h.cell_pm.id, cell.id, notes="Cell scope accepted.")
    assert env.error is None, env.message
    assert env.status == "completed"

    env = await h.c.main_pm_complete(
        UUID(str(root.assigned_to)),
        root.id,
        notes="Root scope complete; escalating to the owner for the final review.",
    )
    assert env.error is None, env.message
    assert env.status == "awaiting_ceo_approval"

    approved = await h.task_service.ceo_approve(root.id, "Reviewed the exported patch.")
    assert approved is not None
    assert str(approved.status) == "completed"
    for task_id in (h.task.id, cell.id, root.id):
        row = await h.task_service.get(task_id)
        assert row is not None
        assert row.pr_number is None
        assert row.pr_url is None
        assert row.branch_name is None


async def test_owner_export_returns_verified_accepted_patches_only(
    harness: LocalReviewHarness,
) -> None:
    h = harness
    cell, root = await _seed_cell_and_root(h)
    await _submit_to_qa(h)
    await _qa_pass(h)
    assert (await h.c.claim_doc_task(h.doc.id, h.task.id)).error is None
    env = await h.c.i_documented(
        h.doc.id,
        h.task.id,
        notes="Documented /healthz in docs/api/health.md",
        files=["docs/api/health.md"],
    )
    assert env.error is None, env.message
    session = h.task_service.session

    assert await h.artifacts.accepted_series_under(root.id) == []
    env = await h.c.complete(h.cell_pm.id, h.task.id, notes="Leaf patch accepted.")
    assert env.error is None, env.message

    series = await h.artifacts.accepted_series_under(root.id)
    assert [item.task_id for item in series] == [h.task.id]
    assert series[0].content.decode() == SYNTHETIC_PATCH
    assert series[0].sha256 == hashlib.sha256(SYNTHETIC_PATCH.encode()).hexdigest()

    stored = await session.get(ReviewArtifactTable, series[0].artifact_id)
    assert stored is not None
    stored.patch = stored.patch + "tampered\n"
    with pytest.raises(ReviewArtifactRefused, match="artifact_digest_mismatch"):
        await h.artifacts.accepted_series_under(root.id)
    assert cell.id != root.id
