"""Pre-submit quality gate: lint + typecheck run in the dev's workspace on
i_am_done, blocking a red submit before it reaches QA. Full tests stay on CI.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from roboco.runtime.subscription.project_executor import (
    CommandOutcome,
    ProjectExecutionUnavailable,
)
from roboco.services.gateway import quality_gate
from roboco.services.gateway.choreographer import Choreographer, ChoreographerDeps
from roboco.services.gateway.choreographer._impl import _IAmDoneContext
from roboco.services.gateway.quality_gate import GateResult, run_quality_commands
from roboco.services.git import GitService

# --- the runner (offline worker executor) -----------------------------------


class _ScriptedExecutor:
    """A ProjectExecutor double: command text -> outcome or raised error."""

    def __init__(
        self, script: dict[str, CommandOutcome | ProjectExecutionUnavailable]
    ) -> None:
        self._script = script
        self.commands: list[str] = []

    async def run_command(self, command: str) -> CommandOutcome:
        self.commands.append(command)
        planned = self._script.get(command, CommandOutcome(0, "", False))
        if isinstance(planned, ProjectExecutionUnavailable):
            raise planned
        return planned


def _ok(output: str = "") -> CommandOutcome:
    return CommandOutcome(exit_code=0, output=output, truncated=False)


def _red(output: str, code: int = 1) -> CommandOutcome:
    return CommandOutcome(exit_code=code, output=output, truncated=False)


@pytest.mark.asyncio
async def test_no_commands_is_an_explicit_skipped_pass() -> None:
    executor = _ScriptedExecutor({})
    result = await run_quality_commands(executor, [])
    assert result.passed is True
    assert result.skipped is True
    assert executor.commands == []


@pytest.mark.asyncio
async def test_all_commands_pass() -> None:
    executor = _ScriptedExecutor({"ruff": _ok("lint-ok")})
    result = await run_quality_commands(
        executor, [("lint", "ruff"), ("typecheck", "mypy")]
    )
    assert result.passed is True
    assert result.skipped is False
    assert result.failures == ()
    assert executor.commands == ["ruff", "mypy"]


@pytest.mark.asyncio
async def test_a_failing_command_blocks_and_is_named() -> None:
    executor = _ScriptedExecutor({"ruff": _red("problem-here")})
    result = await run_quality_commands(
        executor, [("lint", "ruff"), ("typecheck", "mypy")]
    )
    assert result.passed is False
    assert "lint" in result.failures
    assert "problem-here" in result.output


@pytest.mark.asyncio
async def test_every_command_runs_even_after_a_failure() -> None:
    executor = _ScriptedExecutor({"a": _red("AAA"), "b": _red("BBB", 2)})
    result = await run_quality_commands(executor, [("lint", "a"), ("typecheck", "b")])
    assert result.passed is False
    assert set(result.failures) == {"lint", "typecheck"}
    assert "AAA" in result.output
    assert "BBB" in result.output


@pytest.mark.asyncio
async def test_missing_tool_failure_explains_the_offline_toolchain_rule() -> None:
    executor = _ScriptedExecutor({"ruff check": _red("sh: ruff: not found", 127)})
    result = await run_quality_commands(executor, [("lint", "ruff check")])
    assert result.passed is False
    assert "offline" in result.output
    assert "pre-fetched" in result.output


@pytest.mark.asyncio
async def test_infrastructure_error_propagates_and_is_never_a_pass() -> None:
    executor = _ScriptedExecutor(
        {"ruff": _ok(), "mypy": ProjectExecutionUnavailable("run_cancelled")}
    )
    with pytest.raises(ProjectExecutionUnavailable) as raised:
        await run_quality_commands(executor, [("lint", "ruff"), ("typecheck", "mypy")])
    assert raised.value.reason_code == "run_cancelled"


def test_gate_result_summary_and_excerpt() -> None:
    assert GateResult(passed=True).summary == "all checks passed"
    assert GateResult(passed=True, skipped=True).summary.startswith("no quality")
    failed = GateResult(passed=False, failures=("lint",), output="x" * 5000)
    assert "lint" in failed.summary
    # The excerpt is the truncated tail, shorter than the full output.
    assert 0 < len(failed.output_excerpt) < len(failed.output)


def test_quality_gate_module_never_spawns_a_process() -> None:
    source = pathlib.Path(quality_gate.__file__).read_text()
    assert "subprocess" not in source


# --- GitService command selection -------------------------------------------


def test_fast_gate_commands_picks_lint_and_typecheck() -> None:
    project = SimpleNamespace(
        quality_command=None,  # not set → fall back to lint + typecheck
        lint_command="uv run ruff check .",
        typecheck_command="uv run mypy roboco/",
        format_command="uv run ruff format .",  # excluded (mutating)
        test_command="uv run pytest",  # excluded (slow; CI only)
    )
    commands = GitService._fast_gate_commands(project)
    assert commands == [
        ("lint", "uv run ruff check ."),
        ("typecheck", "uv run mypy roboco/"),
    ]


def test_quality_command_takes_precedence_and_runs_alone() -> None:
    """A configured quality_command is the complete fast gate (e.g. it also runs
    complexity/xenon) — it runs alone, not alongside lint/typecheck."""
    project = SimpleNamespace(
        quality_command="make gate",
        lint_command="uv run ruff check .",
        typecheck_command="uv run mypy roboco/",
    )
    assert GitService._fast_gate_commands(project) == [("quality", "make gate")]


def test_fast_gate_commands_empty_when_unconfigured() -> None:
    project = SimpleNamespace(
        quality_command=None, lint_command=None, typecheck_command=None
    )
    assert GitService._fast_gate_commands(project) == []


# --- choreographer glue -----------------------------------------------------


def _choreo(mock_git: MagicMock) -> Choreographer:
    return Choreographer(
        ChoreographerDeps(
            task=MagicMock(),
            work_session=MagicMock(),
            git=mock_git,
            a2a=MagicMock(),
            journal=MagicMock(),
            audit=MagicMock(),
            evidence_repo=MagicMock(),
        )
    )


def _ctx() -> _IAmDoneContext:
    return _IAmDoneContext(
        agent_id=uuid4(),
        task_id=uuid4(),
        task=MagicMock(),
        role_str="developer",
        briefing={},
        notes="self-verification summary",
    )


@pytest.mark.asyncio
async def test_gate_pass_does_not_block() -> None:
    git = MagicMock()
    git.run_pre_submit_quality_gate = AsyncMock(return_value=GateResult(passed=True))
    assert await _choreo(git)._check_quality_gate(_ctx()) is None


@pytest.mark.asyncio
async def test_gate_failure_blocks_with_output_in_remediate() -> None:
    git = MagicMock()
    git.run_pre_submit_quality_gate = AsyncMock(
        return_value=GateResult(
            passed=False, failures=("lint",), output="roboco/x.py:1 E501 line too long"
        )
    )
    env = await _choreo(git)._check_quality_gate(_ctx())
    assert env is not None
    assert env.error == "invalid_state"
    assert "E501" in (env.remediate or "")


@pytest.mark.asyncio
async def test_gate_infrastructure_error_blocks_the_submit() -> None:
    """An unavailable worker must never let a submit through unchecked."""
    git = MagicMock()
    git.run_pre_submit_quality_gate = AsyncMock(
        side_effect=ProjectExecutionUnavailable("launcher_unreachable")
    )
    env = await _choreo(git)._check_quality_gate(_ctx())
    assert env is not None
    assert env.error == "invalid_state"
    assert "could not run" in (env.message or "")
    assert "launcher_unreachable" in (env.remediate or "")


@pytest.mark.asyncio
async def test_gate_unexpected_error_also_blocks_the_submit() -> None:
    git = MagicMock()
    git.run_pre_submit_quality_gate = AsyncMock(
        side_effect=RuntimeError("workspace not found")
    )
    env = await _choreo(git)._check_quality_gate(_ctx())
    assert env is not None
    assert env.error == "invalid_state"


# --- GitService wiring ------------------------------------------------------


def _git_with_project(project: object | None) -> GitService:
    svc = GitService.__new__(GitService)
    svc.log = MagicMock()
    svc._project_for_task = AsyncMock(return_value=project)  # type: ignore[method-assign]
    return svc


@pytest.mark.asyncio
async def test_pre_submit_gate_runs_the_commands_on_the_projects_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = SimpleNamespace(
        id=uuid4(),
        quality_command="make gate",
        lint_command=None,
        typecheck_command=None,
    )
    executor = _ScriptedExecutor({"make gate": _red("E501")})
    provider = MagicMock()
    provider.executor_for = AsyncMock(return_value=executor)
    monkeypatch.setattr(
        "roboco.services.git.project_executor_provider", lambda: provider
    )
    task = SimpleNamespace(id=uuid4())
    result = await _git_with_project(project).run_pre_submit_quality_gate(task)
    provider.executor_for.assert_awaited_once_with(project.id, task.id)
    assert executor.commands == ["make gate"]
    assert result.passed is False


@pytest.mark.asyncio
async def test_pre_submit_gate_raises_when_no_worker_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = SimpleNamespace(
        id=uuid4(),
        quality_command="make gate",
        lint_command=None,
        typecheck_command=None,
    )
    provider = MagicMock()
    provider.executor_for = AsyncMock(
        side_effect=ProjectExecutionUnavailable("worker_not_configured")
    )
    monkeypatch.setattr(
        "roboco.services.git.project_executor_provider", lambda: provider
    )
    with pytest.raises(ProjectExecutionUnavailable):
        await _git_with_project(project).run_pre_submit_quality_gate(
            SimpleNamespace(id=uuid4())
        )


@pytest.mark.asyncio
async def test_pre_submit_gate_with_no_commands_is_a_skip_without_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = SimpleNamespace(
        id=uuid4(), quality_command=None, lint_command=None, typecheck_command=None
    )
    provider = MagicMock()
    provider.executor_for = AsyncMock()
    monkeypatch.setattr(
        "roboco.services.git.project_executor_provider", lambda: provider
    )
    result = await _git_with_project(project).run_pre_submit_quality_gate(
        SimpleNamespace(id=uuid4())
    )
    assert result.skipped is True
    provider.executor_for.assert_not_awaited()
