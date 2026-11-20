"""Run the conventions validator over named files and return a structured outcome.

Shared by the ``python -m roboco.conventions`` CLI and the offline task worker,
so both report identically: findings as one JSON object per line, or a
could-not-run reason (the fail-loud signal the gates block on).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from roboco.foundation.policy.conventions.effective_map import effective_map
from roboco.foundation.policy.conventions.models import (
    ConventionsParseError,
    ConventionsStandard,
)

from .runner import ValidatorCouldNotRun, run
from .scan import derive_from_scan

if TYPE_CHECKING:
    from pathlib import Path

CONVENTIONS_FILE = ".roboco/conventions.yml"


@dataclass(frozen=True)
class CheckOutcome:
    """Finding JSON lines when the validator ran; a reason when it could not."""

    finding_lines: tuple[str, ...] = ()
    could_not_run_reason: str | None = None


def _load_file(root: Path) -> ConventionsStandard | None:
    path = root / CONVENTIONS_FILE
    if not path.is_file():
        return None
    return ConventionsStandard.parse_yaml(path.read_text())


def check_files(root: Path, files: list[str]) -> CheckOutcome:
    """Validate ``files`` (paths relative to ``root``) against the effective map."""
    try:
        file_standard = _load_file(root)
    except ConventionsParseError as exc:
        return CheckOutcome(
            could_not_run_reason=f"unparseable {CONVENTIONS_FILE}: {exc.reason}"
        )
    standard = effective_map(derive_from_scan(root), file_standard)
    try:
        findings = run(root, files, standard)
    except ValidatorCouldNotRun as exc:
        return CheckOutcome(could_not_run_reason=str(exc))
    return CheckOutcome(finding_lines=tuple(f.as_json() for f in findings))
