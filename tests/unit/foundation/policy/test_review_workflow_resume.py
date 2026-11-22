"""Which started tasks may resume ``in_progress`` — pure, no DB/IO."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from roboco.foundation.policy.review_workflow import can_resume_in_progress


def _task(*, workflow: str, branch: str | None, plan: str | None) -> SimpleNamespace:
    return SimpleNamespace(review_workflow=workflow, branch_name=branch, plan=plan)


@pytest.mark.parametrize(
    ("workflow", "branch", "plan", "expected"),
    [
        ("forge", "feature/backend/ABC12345", None, True),
        ("forge", None, "a plan", False),
        ("forge", None, None, False),
        ("local_review", None, "a plan", True),
        ("local_review", None, None, False),
        ("local_review", None, "", False),
        ("local_review", "feature/backend/ABC12345", None, True),
    ],
)
def test_resume_needs_a_branch_or_a_local_review_plan(
    workflow: str, branch: str | None, plan: str | None, expected: bool
) -> None:
    assert (
        can_resume_in_progress(_task(workflow=workflow, branch=branch, plan=plan))
        is expected
    )
