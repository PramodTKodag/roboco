"""Real-DB end-to-end test driving the gateway through the full lifecycle.

The end-to-end integration test that would have caught every smoking
gun in the 2026-05-04 audit. Drives a single task
from pending → completed using a real `db_session` (Postgres-backed
fixture from the top-level conftest), a real `Choreographer`, and a
real `TaskService`. Tasks use the default local review workflow: the
developer's changes are captured as a patch artifact held by a real
`ReviewArtifactService`, and nothing here may touch a forge. The two doubles are
the boundaries that workflow must not cross (`StrictGit` fails the test on any
forge or host-clone call; `FakeWorkspace` stands in for the offline worker).
Journal/A2A/audit/evidence are mocked because they don't gate the lifecycle
paths under test.

When extended to all roles, this test catches:
  - URL prefix mismatch (route-level coverage in test_v1_role_dep)
  - i_will_work_on AttributeError on None (claim → start sequence is real)
  - heartbeat seeding (reaper cutoff)
  - active_claimant_id wired (single-claimant invariant)
  - i_am_done auto-runs submit_verification
  - QA pass clears active_claimant_id
  - a branchless local review claim reaching in_progress
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from roboco.db.tables import AgentTable, ProjectTable, TaskTable
from roboco.models.base import (
    AgentRole,
    AgentStatus,
    TaskNature,
    TaskStatus,
    TaskType,
    Team,
)
from roboco.seeds.initial_data import AGENT_UUIDS
from roboco.services.gateway.choreographer import Choreographer, ChoreographerDeps
from roboco.services.gateway.choreographer import findings as findings_lib
from roboco.services.task import TaskService

from tests.integration.conftest import cleanup_project_durable_rows
from tests.integration.local_review_support import LocalReviewKit

# #172: a developer fresh claim must carry a substantive step checklist.
_STEPS = [
    {
        "title": "Implement the change",
        "description": (
            "edit the target file, add tests, run them, and stage the "
            "change for commit on the task branch"
        ),
    }
]

_GOOD_PLAN = (
    "Implement the task in the workspace: edit the target module, add or "
    "update unit tests covering the change, run the suite locally, then "
    "capture the change for review. Keep the diff focused on the acceptance "
    "criteria and verify it before submitting for QA."
)
_GOOD_TC = ["Follow the existing module's patterns; keep the change minimal."]
_GOOD_RISKS = [
    {
        "risk": "Scope creep balloons the diff and slows review.",
        "mitigation": "Touch only the files the acceptance criteria require.",
    }
]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession


_QA_PASS_NOTES = (
    "Reviewed the local patch artifact; route returns 200 OK with timestamp. "
    "Both acceptance criteria are covered. Approving."
)


def _mock_evidence_repo() -> Any:
    repo = AsyncMock()
    for method in (
        "list_unread_a2a",
        "list_unread_mentions",
        "list_pending_notifications",
        "task_metadata_gaps",
        "recent_team_activity",
        "blockers_in_lane",
        "journal_highlights_for_task",
    ):
        getattr(repo, method).return_value = []
    return repo


def _mock_journal_with_reflect() -> Any:
    """Journal stub that reports reflect/learning/decision entries present.

    ``latest_decision_at`` is anchored to ``datetime.now(UTC)`` so the C8
    recency window on the PM-decision gate accepts it.
    """
    journal = AsyncMock()
    journal.has_reflect_for_task.return_value = True
    journal.has_learning_for_task.return_value = True
    journal.has_decision_for_task.return_value = True
    journal.has_struggle_for_task.return_value = False
    journal.latest_decision_at.return_value = datetime.now(UTC)
    return journal


def _mock_work_session() -> Any:
    """WorkSession stub: empty file list, no unpushed commits."""
    ws = AsyncMock()
    ws.files_changed.return_value = ["roboco/api/routes/health.py"]
    ws.has_unpushed_commits.return_value = False
    return ws


@pytest_asyncio.fixture
async def lifecycle_setup(
    db_session: AsyncSession,
    _test_database_url: str,
) -> AsyncIterator[dict[str, Any]]:
    """Seed a project + dev agent + a single pending task ready to claim."""
    system_agent = AgentTable(
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
    db_session.add(system_agent)
    await db_session.flush()

    project = ProjectTable(
        id=uuid4(),
        name="Lifecycle Test Project",
        slug=f"life-{uuid4().hex[:8]}",
        git_url="https://github.com/example/life.git",
        default_branch="main",
        protected_branches=["main"],
        assigned_cell=Team.BACKEND,
        created_by=system_agent.id,
        is_active=True,
        offline_workspace_handle=uuid4(),
    )
    db_session.add(project)
    await db_session.flush()

    dev_agent = AgentTable(
        id=uuid4(),
        name="BE Dev",
        slug=f"be-dev-{uuid4().hex[:8]}",
        role=AgentRole.DEVELOPER,
        team=Team.BACKEND,
        status=AgentStatus.ACTIVE,
        model_config={},
        system_prompt="dev",
        capabilities=["python"],
        permissions={},
        metrics={},
    )
    qa_agent = AgentTable(
        id=uuid4(),
        name="BE QA",
        slug=f"be-qa-{uuid4().hex[:8]}",
        role=AgentRole.QA,
        team=Team.BACKEND,
        status=AgentStatus.ACTIVE,
        model_config={},
        system_prompt="qa",
        capabilities=["review"],
        permissions={},
        metrics={},
    )
    doc_agent = AgentTable(
        id=uuid4(),
        name="BE Doc",
        slug=f"be-doc-{uuid4().hex[:8]}",
        role=AgentRole.DOCUMENTER,
        team=Team.BACKEND,
        status=AgentStatus.ACTIVE,
        model_config={},
        system_prompt="doc",
        capabilities=["docs"],
        permissions={},
        metrics={},
    )
    cell_pm_agent = AgentTable(
        id=uuid4(),
        name="BE Cell PM",
        slug=f"be-pm-{uuid4().hex[:8]}",
        role=AgentRole.CELL_PM,
        team=Team.BACKEND,
        status=AgentStatus.ACTIVE,
        model_config={},
        system_prompt="cell_pm",
        capabilities=["coord"],
        permissions={},
        metrics={},
    )
    db_session.add_all([dev_agent, qa_agent, doc_agent, cell_pm_agent])
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
        created_by=system_agent.id,
        assigned_to=dev_agent.id,
        acceptance_criteria=["Returns 200", "Includes timestamp"],
        acceptance_criteria_status=[
            {"criterion": "Returns 200", "referencing_artifact_id": "stub"},
            {"criterion": "Includes timestamp", "referencing_artifact_id": "stub"},
        ],
    )
    db_session.add(task)
    await db_session.flush()

    ids: dict[str, Any] = {
        "project_id": project.id,
        "agent_ids": [
            system_agent.id,
            dev_agent.id,
            qa_agent.id,
            doc_agent.id,
            cell_pm_agent.id,
        ],
    }
    try:
        yield {
            "project": project,
            "dev_agent": dev_agent,
            "qa_agent": qa_agent,
            "doc_agent": doc_agent,
            "cell_pm_agent": cell_pm_agent,
            "task": task,
            "kit": LocalReviewKit.create(db_session),
        }
    finally:
        # Roll back db_session's OWN transaction first (releases any lock a
        # post-claim verb call still holds on these rows), THEN delete
        # whatever the claim durability boundary committed for real on a
        # fresh connection. This file's final-stage tests seed ad-hoc extra
        # tasks (the Main-PM coordination root) under the same project, so
        # the cleanup must be project-scoped — every task under the project
        # (children first), its work sessions + audit logs, then the project
        # and the agents. See cleanup_project_durable_rows's own docstring.
        await db_session.rollback()
        await cleanup_project_durable_rows(
            _test_database_url,
            ids["project_id"],
            ids["agent_ids"],
        )


def _uid(value: Any) -> UUID:
    """Cast an ORM row's id column to a real ``UUID`` for static typing."""
    return cast("UUID", value)


def _build_choreographer(
    task_service: TaskService, kit: LocalReviewKit, *, a2a: Any = None
) -> Choreographer:
    """A real Choreographer over local review: artifacts real, forge forbidden."""
    deps = ChoreographerDeps(
        task=task_service,
        work_session=_mock_work_session(),
        git=kit.git,
        a2a=a2a if a2a is not None else AsyncMock(),
        journal=_mock_journal_with_reflect(),
        audit=AsyncMock(),
        evidence_repo=_mock_evidence_repo(),
        review_artifacts=kit.artifacts,
    )
    return Choreographer(deps)


async def _dev_claims_and_submits_to_qa(
    c: Choreographer, task_service: TaskService, task: TaskTable, dev_id: UUID
) -> None:
    """claim → progress → open_pr (captures the artifact) → i_am_done."""
    task_id = _uid(task.id)
    env = await c.i_will_work_on(
        agent_id=dev_id,
        task_id=task_id,
        plan=_GOOD_PLAN,
        steps=_STEPS,
        technical_considerations=_GOOD_TC,
        risks=_GOOD_RISKS,
    )
    assert env.error is None, f"claim failed: {env.message}"
    await task_service.add_progress(task_id, dev_id, "implemented /healthz")
    env = await c.open_pr(dev_id, task_id)
    assert env.error is None, f"open_pr failed: {env.message}"
    # i_am_done obligates the developer's dev_notes section — the agent fills
    # it via note(scope='handoff') first; record_section_note is that write.
    await task_service.record_section_note(
        task_id,
        "developer",
        {"summary": "Implemented /healthz and added a test for the happy path."},
    )
    env = await c.i_am_done(dev_id, task_id, "tests pass; route works")
    assert env.error is None, f"i_am_done failed: {env.message}"
    assert env.status == "awaiting_qa"


async def _qa_passes(c: Choreographer, task: TaskTable, qa_id: UUID) -> None:
    task_id = _uid(task.id)
    env = await c.claim_review(qa_id, task_id)
    assert env.error is None, f"claim_review failed: {env.message}"
    env = await c.pass_review(
        qa_id,
        task_id,
        notes=_QA_PASS_NOTES,
        ac_verdicts=[f"verified: {crit}" for crit in task.acceptance_criteria],
        criteria_verified=[
            {"criterion": crit, "evidence": f"verified in the patch: {crit}"}
            for crit in task.acceptance_criteria
        ],
    )
    assert env.error is None, f"pass_review failed: {env.message}"
    assert env.status == "awaiting_documentation"


async def _documenter_hands_off(
    c: Choreographer, task: TaskTable, doc_id: UUID
) -> None:
    task_id = _uid(task.id)
    env = await c.claim_doc_task(doc_id, task_id)
    assert env.error is None, f"claim_doc_task failed: {env.message}"
    env = await c.i_documented(
        doc_id,
        task_id,
        notes="Documented /healthz behaviour in docs/api/health.md",
        files=["docs/api/health.md"],
    )
    assert env.error is None, f"i_documented failed: {env.message}"
    assert env.status == "awaiting_pm_review"


@pytest.mark.asyncio
async def test_dev_can_claim_pending_task_via_gateway(
    db_session: AsyncSession, lifecycle_setup: dict[str, Any]
) -> None:
    """give_me_work → i_will_work_on lands the task in in_progress.

    Verifies in one shot: None-handling, heartbeat seed, and
    active_claimant_id wired.
    """
    task = lifecycle_setup["task"]
    dev_agent = lifecycle_setup["dev_agent"]
    task_service = TaskService(db_session)
    c = _build_choreographer(task_service, lifecycle_setup["kit"])

    env = await c.i_will_work_on(
        agent_id=dev_agent.id,
        task_id=task.id,
        plan=_GOOD_PLAN,
        steps=_STEPS,
        technical_considerations=_GOOD_TC,
        risks=_GOOD_RISKS,
    )

    assert env.error is None, f"claim failed: {env.message}"
    assert env.status == "in_progress"

    refreshed = await task_service.get(task.id)
    assert refreshed is not None
    assert str(refreshed.status) == "in_progress"
    assert refreshed.assigned_to == dev_agent.id
    assert refreshed.last_heartbeat_at is not None, "heartbeat seed"
    assert refreshed.active_claimant_id == dev_agent.id, "claim lock"


@pytest.mark.asyncio
async def test_dev_full_chain_through_awaiting_qa(
    db_session: AsyncSession, lifecycle_setup: dict[str, Any]
) -> None:
    """claim → open_pr → i_am_done lands in awaiting_qa.

    Drives the full developer-side closure path. Verifies:
      - open_pr captures the local review artifact on the task (no PR, no branch)
      - i_am_done auto-runs submit_verification → verifying → awaiting_qa
      - Heartbeat refreshes after each verb (`_touch`)
      - active_claimant_id remains set through dev's tenure
    """
    task = lifecycle_setup["task"]
    dev_agent = lifecycle_setup["dev_agent"]
    kit: LocalReviewKit = lifecycle_setup["kit"]
    task_service = TaskService(db_session)
    c = _build_choreographer(task_service, kit)

    await _dev_claims_and_submits_to_qa(c, task_service, task, dev_agent.id)

    final = await task_service.get(task.id)
    assert final is not None
    assert str(final.status) == "awaiting_qa"
    assert final.self_verified is True, "self_verified set by auto-verify"
    assert final.pr_number is None, "local review opens no pull request"
    assert final.branch_name is None, "local review cuts no branch"
    artifact = await kit.artifacts.current_for(final)
    assert artifact is not None, "open_pr must capture the review artifact"
    assert artifact.status == "open"
    assert artifact.paths == ["app/health.py"]
    assert kit.git.quality_gate_runs >= 1, "i_am_done runs the worker quality gate"


@pytest.mark.asyncio
async def test_full_chain_through_doc_handoff(
    db_session: AsyncSession, lifecycle_setup: dict[str, Any]
) -> None:
    """Extend the dev chain: QA pass → documenter → awaiting_pm_review.

    Verifies QA pass clears active_claimant_id,
    docs_complete transitions to awaiting_pm_review, and reassignment
    to the cell PM happens on hand-off.
    """
    task = lifecycle_setup["task"]
    dev_agent = lifecycle_setup["dev_agent"]
    qa_agent = lifecycle_setup["qa_agent"]
    doc_agent = lifecycle_setup["doc_agent"]
    cell_pm_agent = lifecycle_setup["cell_pm_agent"]
    kit: LocalReviewKit = lifecycle_setup["kit"]
    task_service = TaskService(db_session)
    c = _build_choreographer(task_service, kit)

    await _dev_claims_and_submits_to_qa(c, task_service, task, dev_agent.id)
    await _qa_passes(c, task, qa_agent.id)

    after_qa = await task_service.get(task.id)
    assert after_qa is not None
    assert after_qa.active_claimant_id is None, (
        "QA pass must clear active_claimant_id for next role"
    )

    await _documenter_hands_off(c, task, doc_agent.id)

    after_docs = await task_service.get(task.id)
    assert after_docs is not None
    assert after_docs.assigned_to == cell_pm_agent.id, (
        "docs_complete must reassign to the cell PM for the team"
    )
    artifact = await kit.artifacts.current_for(after_docs)
    assert artifact is not None
    assert artifact.status == "open", "the reviewed artifact stays open for the PM"


@pytest.mark.asyncio
async def test_dev_claim_reaches_in_progress_without_cutting_a_branch(
    db_session: AsyncSession, lifecycle_setup: dict[str, Any]
) -> None:
    """A branchless pending task (the real shape of a local review task)
    reaches in_progress with no branch cut and no forge call: start() must not
    demand a branch the workflow never creates (the E2E smoke once caught the
    branch-first ordering as "Cannot start work: no branch assigned")."""
    task = lifecycle_setup["task"]
    dev_agent = lifecycle_setup["dev_agent"]
    kit: LocalReviewKit = lifecycle_setup["kit"]
    assert task.branch_name is None
    task_service = TaskService(db_session)

    async def _forbidden_branch_cut(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("a local review claim must not cut a branch")

    with patch.object(TaskService, "_auto_create_branch", _forbidden_branch_cut):
        env = await _build_choreographer(task_service, kit).i_will_work_on(
            agent_id=dev_agent.id,
            task_id=task.id,
            plan=_GOOD_PLAN,
            steps=_STEPS,
            technical_considerations=_GOOD_TC,
            risks=_GOOD_RISKS,
        )
    assert env.error is None, f"claim failed: {env.message}"
    assert env.status == "in_progress"
    refreshed = await task_service.get(task.id)
    assert refreshed is not None
    assert refreshed.branch_name is None
    assert str(refreshed.status) == "in_progress"


async def _seed_main_pm(db_session: AsyncSession) -> UUID:
    """Return the fixed ``main-pm`` agent id, seeding it if not already present.

    Keyed on the fixed foundation UUID: the cross-test shared DB already has
    other tests seeding this exact slug, and an unconditional insert with a
    fresh random id would collide on the slug's unique index.
    """
    main_pm_id = UUID(AGENT_UUIDS["main-pm"])
    if await db_session.get(AgentTable, main_pm_id) is None:
        db_session.add(
            AgentTable(
                id=main_pm_id,
                name="Main PM",
                slug="main-pm",
                role=AgentRole.MAIN_PM,
                team=None,
                status=AgentStatus.ACTIVE,
                model_config={},
                system_prompt="main_pm",
                capabilities=["coord"],
                permissions={},
                metrics={},
            )
        )
        await db_session.flush()
    return main_pm_id


async def _seed_root_task(
    db_session: AsyncSession,
    *,
    project_id: UUID,
    creator_id: UUID,
    main_pm_id: UUID,
) -> TaskTable:
    """Seed a Main-PM coordination root: no parent, owns no PR or branch."""
    root = TaskTable(
        id=uuid4(),
        title="Ship the backend healthz cell",
        description="Coordination root assembling the backend cell's work",
        status=TaskStatus.IN_PROGRESS,
        priority=1,
        task_type=TaskType.PLANNING,
        nature=TaskNature.TECHNICAL,
        team=Team.MAIN_PM,
        project_id=project_id,
        created_by=creator_id,
        assigned_to=main_pm_id,
        parent_task_id=None,
        acceptance_criteria=["Ship the /healthz endpoint end to end"],
    )
    db_session.add(root)
    await db_session.flush()
    return root


async def _seed_cell_and_root(
    db_session: AsyncSession, leaf: TaskTable, cell_pm_id: UUID
) -> tuple[TaskTable, TaskTable]:
    """Parent ``leaf`` under a cell task under a Main-PM root."""
    main_pm_id = await _seed_main_pm(db_session)
    root = await _seed_root_task(
        db_session,
        project_id=_uid(leaf.project_id),
        creator_id=_uid(leaf.created_by),
        main_pm_id=main_pm_id,
    )
    cell = TaskTable(
        id=uuid4(),
        title="Backend cell: healthz",
        description="Cell scope assembling the /healthz work",
        status=TaskStatus.IN_PROGRESS,
        priority=2,
        task_type=TaskType.PLANNING,
        nature=TaskNature.TECHNICAL,
        team=Team.BACKEND,
        project_id=_uid(leaf.project_id),
        created_by=leaf.created_by,
        assigned_to=cell_pm_id,
        parent_task_id=_uid(root.id),
        acceptance_criteria=["The /healthz endpoint ships"],
    )
    db_session.add(cell)
    await db_session.flush()
    leaf.parent_task_id = cell.id
    await db_session.flush()
    return cell, root


async def _leaf_reviewed_documented_and_accepted(
    c: Choreographer, lifecycle_setup: dict[str, Any], task_service: TaskService
) -> None:
    """Drive the leaf dev → QA → documenter → cell PM accept chain."""
    leaf = lifecycle_setup["task"]
    await _dev_claims_and_submits_to_qa(
        c, task_service, leaf, _uid(lifecycle_setup["dev_agent"].id)
    )
    await _qa_passes(c, leaf, _uid(lifecycle_setup["qa_agent"].id))
    await _documenter_hands_off(c, leaf, _uid(lifecycle_setup["doc_agent"].id))
    env = await c.complete(
        _uid(lifecycle_setup["cell_pm_agent"].id),
        _uid(leaf.id),
        notes="Local patch reviewed by QA and documented; accepting it.",
    )
    assert env.error is None, f"complete (leaf) failed: {env.message}"
    assert env.status == "completed"


async def _cell_submit_up_and_complete(
    c: Choreographer, cell_pm_id: UUID, cell_id: UUID
) -> None:
    """submit_up lands in PM review (no PR-review gate); the cell PM completes."""
    env = await c.submit_up(
        cell_pm_id,
        cell_id,
        notes="Cell scope assembled: /healthz is implemented, reviewed and documented.",
    )
    assert env.error is None, f"submit_up failed: {env.message}"
    assert env.status == "awaiting_pm_review", (
        "local review has no PR to gate: submit_up goes straight to PM review"
    )
    env = await c.complete(
        cell_pm_id, cell_id, notes="Cell scope reviewed and approved; accepting it."
    )
    assert env.error is None, f"complete (cell) failed: {env.message}"
    assert env.status == "completed"


@pytest.mark.asyncio
async def test_cell_and_root_reach_completed_via_pm_review_and_ceo_approval(
    db_session: AsyncSession, lifecycle_setup: dict[str, Any]
) -> None:
    """Extends the dev/QA/doc chain through the assembly stages: leaf accepted
    -> cell_pm submit_up -> PM review -> cell_pm complete -> main_pm
    submit_root -> main_pm complete -> CEO approval, reaching ``completed``.

    Seeds a real root (main_pm) + cell (cell_pm) task hierarchy over the leaf
    and drives every stage through the real Choreographer + TaskService +
    ReviewArtifactService + DB. No stage opens a pull request or touches a
    forge: the leaf's accepted artifact is the only deliverable, and the CEO's
    approval completes the root.
    """
    leaf = lifecycle_setup["task"]
    cell_pm_agent = lifecycle_setup["cell_pm_agent"]
    kit: LocalReviewKit = lifecycle_setup["kit"]
    task_service = TaskService(db_session)
    c = _build_choreographer(task_service, kit)
    cell, root = await _seed_cell_and_root(db_session, leaf, _uid(cell_pm_agent.id))
    cell_id = _uid(cell.id)
    root_id = _uid(root.id)
    main_pm_id = _uid(root.assigned_to)

    await _leaf_reviewed_documented_and_accepted(c, lifecycle_setup, task_service)
    accepted = await kit.artifacts.current_for(leaf)
    assert accepted is not None
    assert accepted.status == "accepted"
    assert kit.workspace.accepted == 1, "acceptance advances the workspace baseline"

    await _cell_submit_up_and_complete(c, _uid(cell_pm_agent.id), cell_id)
    # The root's only subtask (the cell) is now terminal.
    assert await task_service.all_subtasks_terminal(root_id)

    env = await c.submit_root(
        main_pm_id,
        root_id,
        notes="Root scope assembled: the backend cell's /healthz slice is"
        " accepted; submitting the root for the final review.",
    )
    assert env.error is None, f"submit_root failed: {env.message}"
    assert env.status == "awaiting_pm_review", (
        "local review has no PR to gate: submit_root goes straight to PM review"
    )

    premature = await task_service.ceo_approve(
        root_id, "Attempting approval before the root is escalated to the CEO."
    )
    assert premature is None, "ceo_approve must refuse a root not awaiting the CEO"

    env = await c.main_pm_complete(
        main_pm_id,
        root_id,
        notes="Root scope reviewed and approved; escalating to the CEO for"
        " final sign-off.",
    )
    assert env.error is None, f"main_pm_complete failed: {env.message}"
    assert env.status == "awaiting_ceo_approval"

    root_awaiting_ceo = await task_service.get(root_id)
    assert root_awaiting_ceo is not None
    assert str(root_awaiting_ceo.status) == "awaiting_ceo_approval"
    assert root_awaiting_ceo.assigned_to is None, (
        "main_pm_complete clears assigned_to so no agent is respawned while"
        " the CEO decides"
    )

    approved = await task_service.ceo_approve(
        root_id,
        "Approved: the backend cell's /healthz slice is complete, reviewed,"
        " and accepted. Shipping to production.",
    )
    assert approved is not None, "ceo_approve refused an escalated local review root"
    assert str(approved.status) == "completed"

    for task_id in (_uid(leaf.id), cell_id, root_id):
        row = await task_service.get(task_id)
        assert row is not None
        assert str(row.status) == "completed"
        assert row.pr_number is None
        assert row.pr_url is None
        assert row.branch_name is None


@pytest.mark.asyncio
async def test_pm_request_changes_on_submit_up_then_resubmit_reaches_completed(
    db_session: AsyncSession, lifecycle_setup: dict[str, Any]
) -> None:
    """Reject path: submit_up -> request_changes -> needs_revision -> cell PM
    re-claims (``i_will_plan``) -> resubmits naming the resolved finding ->
    complete, reaching the terminal ``completed`` outcome.

    The local equivalent of the retired in-path PR-review gate's reject leg:
    with no pull request to gate, the bounce of an assembled cell is the PM's
    ``request_changes``, which still ledgers its findings and still requires
    every open one to be named before the resubmit is accepted.
    """
    leaf = lifecycle_setup["task"]
    cell_pm_agent = lifecycle_setup["cell_pm_agent"]
    kit: LocalReviewKit = lifecycle_setup["kit"]
    task_service = TaskService(db_session)
    c = _build_choreographer(task_service, kit)
    cell, _root = await _seed_cell_and_root(db_session, leaf, _uid(cell_pm_agent.id))
    cell_id = _uid(cell.id)
    cell_pm_id = _uid(cell_pm_agent.id)
    await _leaf_reviewed_documented_and_accepted(c, lifecycle_setup, task_service)

    env = await c.submit_up(
        cell_pm_id,
        cell_id,
        notes="Cell scope assembled: /healthz implemented; submitting the cell"
        " for review.",
    )
    assert env.error is None, f"submit_up failed: {env.message}"
    assert env.status == "awaiting_pm_review"

    env = await c.request_changes(
        cell_pm_id,
        cell_id,
        findings=[
            {
                "severity": "major",
                "expected": "the /healthz handler returns a timestamp field",
                "actual": "the /healthz handler does not return a timestamp field"
                " as the acceptance criteria require",
            }
        ],
    )
    assert env.error is None, f"request_changes failed: {env.message}"
    assert env.status == "needs_revision"

    failed = await task_service.get(cell_id)
    assert failed is not None
    assert failed.assigned_to == cell_pm_agent.id, (
        "request_changes hands the assembled task back to its owning cell PM"
    )
    assert "timestamp field" in (failed.pm_notes or "")
    open_findings = await findings_lib.open_findings_for_task(db_session, cell_id)
    assert len(open_findings) == 1, "request_changes must ledger exactly one finding"
    finding_id = str(open_findings[0].id)[:8]

    env = await c.i_will_plan(
        cell_pm_id,
        cell_id,
        plan="Re-claim the rejected cell scope and resubmit after fixing the"
        " missing timestamp field.",
        rich_plan={
            "approach": (
                "Address the reviewer's finding: the /healthz handler must"
                " return a timestamp field. Re-verify both acceptance"
                " criteria locally, then resubmit the cell for another"
                " review pass."
            ),
            "sub_tasks": [
                {
                    "title": "Fix missing timestamp field",
                    "description": (
                        "Update the /healthz handler to include a timestamp"
                        " field in its response body, matching the second"
                        " acceptance criterion the reviewer flagged."
                    ),
                },
            ],
        },
    )
    assert env.error is None, f"i_will_plan (re-claim) failed: {env.message}"
    assert env.status == "in_progress"

    reclaimed = await task_service.get(cell_id)
    assert reclaimed is not None
    assert str(reclaimed.status) == "in_progress"
    assert reclaimed.assigned_to == cell_pm_agent.id

    # Resubmit, naming the resolved finding (FINDINGS_ADDRESSED requires every
    # open ledger finding to be named before a re-submit is allowed).
    env = await c.submit_up(
        cell_pm_id,
        cell_id,
        notes="Fixed the missing timestamp field per the finding; resubmitting"
        " the cell for review.",
        resolved_findings=[
            {
                "finding_id": finding_id,
                "note": "Added the timestamp field to the /healthz response.",
            }
        ],
    )
    assert env.error is None, f"resubmit failed: {env.message}"
    assert env.status == "awaiting_pm_review"

    env = await c.complete(
        cell_pm_id,
        cell_id,
        notes="Cell scope reviewed and approved after revision; accepting it.",
    )
    assert env.error is None, f"complete (cell) failed: {env.message}"
    final_cell = await task_service.get(cell_id)
    assert final_cell is not None
    assert str(final_cell.status) == "completed"
