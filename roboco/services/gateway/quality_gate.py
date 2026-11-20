"""Run a project's fast quality gate in the project's offline worker workspace.

Invoked by the developer's ``i_am_done`` submit so a red gate is caught at the
dev's desk instead of surfacing in QA review or CI. The gate runs the project's
configured *non-mutating* fast checks (lint, typecheck); the slow test suite
intentionally stays on CI. Commands run only through a ``ProjectExecutor`` (the
credential-free, network-less worker), never in this process. The gate is
fail-closed: a failing check blocks, and so does any infrastructure error
(``ProjectExecutionUnavailable`` propagates), so an unavailable worker can never
read as a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from roboco.runtime.subscription.project_executor import CommandRunner

# Cap the remediate excerpt so a huge lint dump doesn't bloat the envelope.
_OUTPUT_EXCERPT_CHARS = 2000


@dataclass(frozen=True)
class GateResult:
    """Outcome of a pre-submit quality gate run."""

    passed: bool
    skipped: bool = False
    failures: tuple[str, ...] = ()
    output: str = ""

    @property
    def summary(self) -> str:
        if self.skipped:
            return "no quality commands configured (skipped)"
        if self.passed:
            return "all checks passed"
        return f"failed: {', '.join(self.failures)}"

    @property
    def output_excerpt(self) -> str:
        """The tail of the combined output (where errors usually are)."""
        return self.output[-_OUTPUT_EXCERPT_CHARS:]


_MISSING_TOOL_EXIT_CODE = 127
_MISSING_TOOL_HINT = (
    "the command's tool is not available in the offline worker. Project "
    "dependencies are never installed on demand; provide the toolchain as "
    "pre-fetched, locked packages in the project workspace, then retry"
)


async def run_quality_commands(
    executor: CommandRunner, commands: list[tuple[str, str]]
) -> GateResult:
    """Run each ``(name, command)`` through ``executor`` and aggregate the result.

    Every command runs (we don't stop at the first failure) so the developer
    sees all gate failures in one shot. A non-zero exit is a failure; its
    combined output is captured for the remediate hint. An empty command list is
    the one explicit skip (the project configured no fast gate). Executor errors
    propagate: they are never converted into a pass or a skip.
    """
    if not commands:
        return GateResult(passed=True, skipped=True)
    failures: list[str] = []
    chunks: list[str] = []
    for name, command in commands:
        outcome = await executor.run_command(command)
        chunks.append(f"$ {command}\n{outcome.output.strip()}")
        if outcome.exit_code != 0:
            failures.append(name)
            if outcome.exit_code == _MISSING_TOOL_EXIT_CODE:
                chunks.append(f"{name}: {_MISSING_TOOL_HINT}")
    return GateResult(
        passed=not failures,
        failures=tuple(failures),
        output="\n\n".join(chunks),
    )
