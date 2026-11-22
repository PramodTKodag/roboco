"""Conventions check for a local review task reads the artifact, never a branch.

A local review task has no branch or remote, so the checked files come from its
stored review artifact and run in the offline worker. Anything that stops the
check from running must block the gate, never pass it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from roboco.services import review_artifact as review_artifact_module
from roboco.services.git import GitService
from roboco.services.review_artifact import ReviewArtifactRefused


def _local_review_task() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), review_workflow="local_review", branch_name=None)


def _service_with_artifact_files(
    monkeypatch: pytest.MonkeyPatch, present_files: AsyncMock
) -> GitService:
    artifacts = MagicMock(present_files=present_files)
    monkeypatch.setattr(
        review_artifact_module,
        "get_review_artifact_service",
        lambda _session: artifacts,
    )
    service = GitService(MagicMock())
    object.__setattr__(
        service,
        "list_changed_files",
        AsyncMock(side_effect=AssertionError("a local review task has no branch")),
    )
    return service


@pytest.mark.asyncio
async def test_artifact_files_are_validated_in_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_artifact_files(
        monkeypatch, AsyncMock(return_value=["app/a.py", "app/b.py"])
    )
    validator = AsyncMock(return_value={"findings": [], "could_not_run": False})
    object.__setattr__(service, "_run_conventions_validator", validator)
    task = _local_review_task()

    result = await service.conventions_check_for_task(uuid4(), task, timeout=7.0)

    assert result == {"findings": [], "could_not_run": False}
    validator.assert_awaited_once_with(task, ["app/a.py", "app/b.py"], timeout=7.0)


@pytest.mark.asyncio
async def test_an_artifact_with_no_present_files_has_nothing_to_validate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_artifact_files(monkeypatch, AsyncMock(return_value=[]))
    validator = AsyncMock()
    object.__setattr__(service, "_run_conventions_validator", validator)

    result = await service.conventions_check_for_task(uuid4(), _local_review_task())

    assert result == {"findings": [], "could_not_run": False}
    validator.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unreadable_artifact_fails_closed_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_artifact_files(
        monkeypatch,
        AsyncMock(side_effect=ReviewArtifactRefused("digest_mismatch")),
    )
    validator = AsyncMock()
    object.__setattr__(service, "_run_conventions_validator", validator)

    result = await service.conventions_check_for_task(uuid4(), _local_review_task())

    assert result["could_not_run"] is True
    assert result["findings"] == []
    assert "digest_mismatch" in result["reason"]
    validator.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_task_with_no_artifact_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_with_artifact_files(
        monkeypatch, AsyncMock(side_effect=ReviewArtifactRefused("no_artifact"))
    )

    result = await service.conventions_check_for_task(uuid4(), _local_review_task())

    assert result["could_not_run"] is True
    assert "no_artifact" in result["reason"]
