"""conventions_check_for_task must fail CLOSED on a resolution error.

The conventions block gate (i_am_done / pr_pass) treats
``could_not_run=False`` + no findings as a clean PASS. A workspace or diff
resolution error that returns ``could_not_run=False`` therefore silently
disables the block gate — the opposite of the validator's OWN fail-loud
philosophy (exit 3 → ``could_not_run=True`` → gate blocks, never silently
passes). A raised exception during workspace/diff resolution is a real error,
not an empty result, so it must fail closed: ``could_not_run=True``.

The two LEGITIMATE fail-open paths stay fail-open:
- no ``branch_name`` (a branchless/coordination task has nothing to validate)
- no changed files (nothing changed → nothing to validate)
These are empty-result cases, not errors, so the gate correctly passes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from roboco.runtime.subscription.project_executor import (
    ConventionsOutcome,
    ProjectExecutionUnavailable,
)
from roboco.services import git as git_module
from roboco.services.git import GitService


def _service() -> GitService:
    return GitService(MagicMock())


def _bind(svc: GitService, name: str, value: object) -> None:
    object.__setattr__(svc, name, value)


def _task(branch_name: str) -> MagicMock:
    return MagicMock(branch_name=branch_name)


@pytest.mark.asyncio
async def test_diff_resolution_error_fails_closed() -> None:
    """``list_changed_files`` raising → could_not_run=True (block), not a
    silent pass. The workspace resolved fine, but the diff itself failed."""
    svc = _service()
    _bind(
        svc,
        "list_changed_files",
        AsyncMock(side_effect=RuntimeError("git diff errored")),
    )
    result = await svc.conventions_check_for_task(uuid4(), _task("feature/backend/abc"))
    assert result["could_not_run"] is True
    assert result["findings"] == []


@pytest.mark.asyncio
async def test_no_branch_still_fails_open() -> None:
    """A branchless/coordination task (no branch_name) has nothing to
    validate — the gate correctly passes (could_not_run=False). This is NOT
    an error; it must stay fail-open."""
    svc = _service()
    result = await svc.conventions_check_for_task(uuid4(), _task(""))
    assert result["could_not_run"] is False
    assert result["findings"] == []


@pytest.mark.asyncio
async def test_no_changed_files_still_fails_open() -> None:
    """A task whose branch resolved but has no changed files has nothing to
    validate — the gate correctly passes (could_not_run=False). This is an
    empty result, not an error; it must stay fail-open."""
    svc = _service()
    _bind(svc, "list_changed_files", AsyncMock(return_value=[]))
    result = await svc.conventions_check_for_task(uuid4(), _task("feature/backend/abc"))
    assert result["could_not_run"] is False
    assert result["findings"] == []


@pytest.mark.asyncio
async def test_preferred_parent_forwards_to_list_changed_files() -> None:
    """The in-path PR-review gate's cross-team parent (see ``diff``) must
    reach ``list_changed_files`` so the validator never analyzes files
    inherited from the wrong-team derived base."""
    svc = _service()
    changed = AsyncMock(return_value=[])
    _bind(svc, "list_changed_files", changed)
    actor_id = uuid4()
    await svc.conventions_check_for_task(
        actor_id,
        _task("feature/frontend/root--cell"),
        preferred_parent="feature/main_pm/root",
    )
    changed.assert_awaited_once_with(
        branch_name="feature/frontend/root--cell",
        actor_agent_id=actor_id,
        preferred_parent="feature/main_pm/root",
    )


@pytest.mark.asyncio
async def test_changed_files_kwarg_skips_list_changed_files_resolution() -> None:
    """A caller that already resolved the changed-file list (e.g. via
    ``diff_and_files``) threads it straight in via ``changed_files`` — this
    must skip the internal ``list_changed_files`` call (a THIRD resolution
    on top of the caller's own diff/files legs) entirely."""
    svc = _service()
    changed = AsyncMock(return_value=["should-not-be-called.py"])
    _bind(svc, "list_changed_files", changed)
    validator = AsyncMock(return_value={"findings": [], "could_not_run": False})
    _bind(svc, "_run_conventions_validator", validator)

    task = _task_with_id("feature/backend/abc")
    await svc.conventions_check_for_task(
        uuid4(), task, changed_files=["already-resolved.py"]
    )
    changed.assert_not_awaited()
    validator.assert_awaited_once_with(task, ["already-resolved.py"], timeout=None)


@pytest.mark.asyncio
async def test_changed_files_kwarg_omitted_keeps_self_resolving_default() -> None:
    """Omitting ``changed_files`` (every existing caller) keeps the prior
    self-resolving behavior byte-for-byte."""
    svc = _service()
    changed = AsyncMock(return_value=["a.py"])
    _bind(svc, "list_changed_files", changed)
    validator = AsyncMock(return_value={"findings": [], "could_not_run": False})
    _bind(svc, "_run_conventions_validator", validator)

    task = _task_with_id("feature/backend/abc")
    await svc.conventions_check_for_task(uuid4(), task)
    changed.assert_awaited_once()
    validator.assert_awaited_once_with(task, ["a.py"], timeout=None)


class _StubExecutor:
    def __init__(self, behaviour: object) -> None:
        self._behaviour = behaviour
        self.files: list[str] = []

    async def check_conventions(self, files: list[str]) -> ConventionsOutcome:
        self.files = files
        if isinstance(self._behaviour, BaseException):
            raise self._behaviour
        if callable(self._behaviour):
            outcome = await self._behaviour()
            assert isinstance(outcome, ConventionsOutcome)
            return outcome
        assert isinstance(self._behaviour, ConventionsOutcome)
        return self._behaviour


def _validator_service(
    monkeypatch: pytest.MonkeyPatch, executor: object | Exception
) -> tuple[GitService, MagicMock]:
    svc = _service()
    project = MagicMock(id=uuid4())
    _bind(svc, "_project_for_task", AsyncMock(return_value=project))
    provider = MagicMock()
    if isinstance(executor, Exception):
        provider.executor_for = AsyncMock(side_effect=executor)
    else:
        provider.executor_for = AsyncMock(return_value=executor)
    monkeypatch.setattr(git_module, "project_executor_provider", lambda: provider)
    return svc, provider


@pytest.mark.asyncio
async def test_validator_runs_in_the_worker_and_parses_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = _StubExecutor(ConventionsOutcome(finding_lines=('{"rule":"r"}', "junk")))
    svc, provider = _validator_service(monkeypatch, executor)
    task = _task_with_id("feature/backend/abc")
    result = await svc._run_conventions_validator(task, ["a.py"])
    assert result == {
        "findings": [{"rule": "r"}],
        "could_not_run": False,
        "reason": None,
    }
    assert executor.files == ["a.py"]
    provider.executor_for.assert_awaited_once()


@pytest.mark.asyncio
async def test_validator_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung validator must time out and fail closed (could_not_run=True so the
    block gate refuses the submit), never hang the gate or pass silently."""

    async def _hang() -> ConventionsOutcome:
        await asyncio.sleep(30)
        return ConventionsOutcome()

    svc, _ = _validator_service(monkeypatch, _StubExecutor(_hang))
    monkeypatch.setattr(git_module, "_CONVENTIONS_VALIDATOR_TIMEOUT_SECONDS", 0.01)
    result = await svc._run_conventions_validator(_task_with_id("b"), ["a.py"])
    assert result["could_not_run"] is True
    assert "timed out" in (result.get("reason") or "")


@pytest.mark.asyncio
async def test_validator_timeout_override_used_over_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``timeout`` wins over the module default: the advisory
    claim_review path's shorter budget must actually bound the wait. The module
    constant is set LONG so a failure proves the override was ignored."""

    async def _hang() -> ConventionsOutcome:
        await asyncio.sleep(30)
        return ConventionsOutcome()

    svc, _ = _validator_service(monkeypatch, _StubExecutor(_hang))
    monkeypatch.setattr(git_module, "_CONVENTIONS_VALIDATOR_TIMEOUT_SECONDS", 300)
    result = await svc._run_conventions_validator(
        _task_with_id("b"), ["a.py"], timeout=0.01
    )
    assert result["could_not_run"] is True
    assert "timed out after 0.01s" in (result.get("reason") or "")


@pytest.mark.asyncio
async def test_validator_that_could_not_run_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _ = _validator_service(
        monkeypatch, _StubExecutor(ConventionsOutcome(reason="no grammar"))
    )
    result = await svc._run_conventions_validator(_task_with_id("b"), ["a.py"])
    assert result["could_not_run"] is True
    assert result["reason"] == "no grammar"


@pytest.mark.asyncio
async def test_unavailable_worker_fails_closed_at_executor_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _ = _validator_service(
        monkeypatch, ProjectExecutionUnavailable("worker_not_configured")
    )
    result = await svc._run_conventions_validator(_task_with_id("b"), ["a.py"])
    assert result["could_not_run"] is True
    assert "worker_not_configured" in result["reason"]


@pytest.mark.asyncio
async def test_worker_failure_during_the_check_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _ = _validator_service(
        monkeypatch, _StubExecutor(ProjectExecutionUnavailable("run_cancelled"))
    )
    result = await svc._run_conventions_validator(_task_with_id("b"), ["a.py"])
    assert result["could_not_run"] is True
    assert "run_cancelled" in result["reason"]


@pytest.mark.asyncio
async def test_task_without_a_project_fails_closed() -> None:
    svc = _service()
    _bind(svc, "_project_for_task", AsyncMock(return_value=None))
    result = await svc._run_conventions_validator(_task_with_id("b"), ["a.py"])
    assert result["could_not_run"] is True


def _task_with_id(branch_name: str) -> MagicMock:
    """Like ``_task`` but with a real UUID id — ``conventions_check_for_task``
    calls ``require_uuid(task.id)`` outside the resolution try/except, so a
    bare MagicMock id would raise before reaching the validator call."""
    return MagicMock(branch_name=branch_name, id=uuid4())


@pytest.mark.asyncio
async def test_conventions_check_for_task_forwards_timeout_override() -> None:
    """``conventions_check_for_task``'s ``timeout`` kwarg must reach
    ``_run_conventions_validator`` — the seam ``claim_review`` uses to pin
    the ADVISORY (shorter) budget instead of the fail-closed default."""
    svc = _service()
    _bind(svc, "list_changed_files", AsyncMock(return_value=["a.py"]))
    validator = AsyncMock(return_value={"findings": [], "could_not_run": False})
    _bind(svc, "_run_conventions_validator", validator)

    task = _task_with_id("feature/backend/abc")
    await svc.conventions_check_for_task(uuid4(), task, timeout=30.0)
    validator.assert_awaited_once_with(task, ["a.py"], timeout=30.0)


@pytest.mark.asyncio
async def test_conventions_check_for_task_default_timeout_is_none() -> None:
    """The fail-closed callers (i_am_done's ``_conventions_gate``, pr_pass's
    ``_conventions_guard``) never pass ``timeout`` — confirming the default
    forwards ``None`` so ``_run_conventions_validator`` falls back to its
    hardcoded fail-closed cap, unchanged."""
    svc = _service()
    _bind(svc, "list_changed_files", AsyncMock(return_value=["a.py"]))
    validator = AsyncMock(return_value={"findings": [], "could_not_run": False})
    _bind(svc, "_run_conventions_validator", validator)

    task = _task_with_id("feature/backend/abc")
    await svc.conventions_check_for_task(uuid4(), task)
    validator.assert_awaited_once_with(task, ["a.py"], timeout=None)
