"""DepUpdateEngine — no upgrade probe exists, so nothing is ever originated.

Detecting an upgrade means running a package manager against a registry. That
cannot run on the controller (project code) nor in the network-less worker, so the
engine refuses to originate and says so loudly instead of guessing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from roboco.config import settings
from roboco.services.dep_update_engine import DepUpdateEngine

if TYPE_CHECKING:
    from roboco.db.tables import TaskTable


def _project(slug: str, command: str | None = "uv lock -U") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        slug=slug,
        git_url=f"https://example.invalid/{slug}.git",
        dep_update_command=command,
    )


@pytest.fixture
def _enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dep_update_enabled", True)
    monkeypatch.setattr(settings, "dep_update_max_per_cycle", 10)
    monkeypatch.setattr(settings, "dep_update_max_open_tasks", 10)


def _engine() -> DepUpdateEngine:
    engine = DepUpdateEngine(AsyncMock())
    object.__setattr__(engine, "log", MagicMock())
    return engine


async def _run(
    engine: DepUpdateEngine, task_svc: MagicMock, projects: list[SimpleNamespace]
) -> list[TaskTable]:
    with (
        patch(
            "roboco.services.dep_update_engine.get_task_service",
            return_value=task_svc,
        ),
        patch(
            "roboco.services.maintenance_pause.is_paused",
            AsyncMock(return_value=False),
        ),
    ):
        return await engine.run_cycle(projects)


@pytest.mark.asyncio
async def test_no_task_is_originated_and_the_refusal_is_logged(_enabled: None) -> None:
    task_svc = MagicMock()
    task_svc.list_open_dep_update_tasks = AsyncMock(return_value=[])
    task_svc.create = AsyncMock()
    engine = _engine()

    projects = [_project("a"), _project("b")]
    created = await _run(engine, task_svc, projects)

    assert created == []
    task_svc.create.assert_not_awaited()
    assert engine.log.error.call_count == len(projects)


@pytest.mark.asyncio
async def test_a_project_without_a_command_is_skipped_silently(_enabled: None) -> None:
    task_svc = MagicMock()
    task_svc.list_open_dep_update_tasks = AsyncMock(return_value=[])
    engine = _engine()

    assert await _run(engine, task_svc, [_project("a", command=None)]) == []
    engine.log.error.assert_not_called()
