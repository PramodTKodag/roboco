"""roboco/foundation/policy/review_workflow.py

Pure rules for how a task's work is reviewed and handed to the owner.

``local_review`` is the only workflow that can start outbound-free work: the
developer's change is captured as a local patch artifact (an identifier plus a
SHA-256 digest of the bytes), QA and the owner review that artifact, and the
owner exports it. Nothing is pushed, opened as a pull request or merged.

``forge`` marks history created before this workflow existed. Such a task keeps
its recorded pull-request data for reading, but no verb may act on a forge.
Stdlib only, no IO.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class ReviewWorkflow(StrEnum):
    LOCAL_REVIEW = "local_review"
    FORGE = "forge"


class ArtifactStatus(StrEnum):
    """Lifecycle of one captured patch: it is decided exactly once."""

    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


DEFAULT_REVIEW_WORKFLOW: Final[ReviewWorkflow] = ReviewWorkflow.LOCAL_REVIEW
ARTIFACT_ID_PREFIX_LENGTH: Final[int] = 8


def is_local_review(task: object) -> bool:
    """True when the task uses the local patch-artifact workflow."""
    return str(getattr(task, "review_workflow", "")) == ReviewWorkflow.LOCAL_REVIEW


def forbids_forge_actions(task: object) -> bool:
    """True when the task's recorded workflow bars any push, PR or merge.

    Every stored task carries a workflow, and neither one may act on a code host:
    ``local_review`` never uses one and ``forge`` history is read-only.
    """
    return str(getattr(task, "review_workflow", "")) in {w.value for w in ReviewWorkflow}


def artifact_label(artifact_id: object, digest: str) -> str:
    """Short, stable label an agent or owner can quote: ``<id8>@<sha8>``."""
    return f"{str(artifact_id)[:ARTIFACT_ID_PREFIX_LENGTH]}@{digest[:8]}"
