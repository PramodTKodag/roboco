"""Startup reconcile keeps started local review work and rolls back true orphans.

A local review task is branchless by design, so a missing branch must not read as
an orphaned claim after a controller restart.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from roboco.runtime.orchestrator import AgentOrchestrator


def _task(*, workflow: str, branch: str | None, plan: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        status="in_progress",
        review_workflow=workflow,
        branch_name=branch,
        plan=plan,
    )


@pytest.mark.asyncio
async def test_started_local_review_task_survives_restart() -> None:
    started_local = _task(workflow="local_review", branch=None, plan="the plan")
    started_forge = _task(workflow="forge", branch="feature/backend/A1", plan=None)
    orphan_forge = _task(workflow="forge", branch=None, plan="the plan")
    unplanned_local = _task(workflow="local_review", branch=None, plan=None)
    service = SimpleNamespace(
        list_in_progress_or_claimed=AsyncMock(
            return_value=[started_local, started_forge, orphan_forge, unplanned_local]
        ),
        unclaim_for_reaper=AsyncMock(),
    )
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)

    await orchestrator._reconcile_with_service(service)  # type: ignore[arg-type]

    rolled_back = [call.args[0] for call in service.unclaim_for_reaper.await_args_list]
    assert rolled_back == [orphan_forge.id, unplanned_local.id]
