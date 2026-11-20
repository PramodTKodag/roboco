"""DepUpdateEngine against a real session.

No upgrade probe exists (it needs a package manager and a registry, which neither
the controller nor the offline worker may use), so an enabled engine originates
nothing; a disabled engine is a no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from roboco.config import settings
from roboco.db.tables import AgentTable, ProjectTable
from roboco.foundation import identity as _foundation
from roboco.models.base import AgentRole, AgentStatus, Team
from roboco.services.dep_update_engine import get_dep_update_engine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

SYSTEM_UUID = _foundation.AGENTS["system"].uuid
MAIN_PM_UUID = _foundation.AGENTS["main-pm"].uuid


async def _get_or_create_agent(
    db: AsyncSession, agent_id: object, role: AgentRole, slug: str
) -> None:
    if await db.get(AgentTable, agent_id) is None:
        db.add(
            AgentTable(
                id=agent_id,
                name=slug,
                slug=f"{slug}-{uuid4().hex[:8]}",
                role=role,
                team=None,
                status=AgentStatus.ACTIVE,
                model_config={},
                system_prompt="x",
                capabilities=[],
                permissions={},
                metrics={},
            )
        )
        await db.flush()


async def _seed_project(
    db: AsyncSession, slug: str, git_url: str, *, command: str | None = "uv lock -U"
) -> ProjectTable:
    project = ProjectTable(
        id=uuid4(),
        name=slug,
        slug=slug,
        git_url=git_url,
        assigned_cell=Team.BACKEND,
        created_by=SYSTEM_UUID,
        dep_update_command=command,
    )
    db.add(project)
    await db.flush()
    return project


@pytest.fixture(autouse=True)
async def _enabled(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dep_update_enabled", True)
    monkeypatch.setattr(settings, "dep_update_max_per_cycle", 5)
    monkeypatch.setattr(settings, "dep_update_max_open_tasks", 5)
    await _get_or_create_agent(db_session, SYSTEM_UUID, AgentRole.SYSTEM, "system")
    await _get_or_create_agent(db_session, MAIN_PM_UUID, AgentRole.MAIN_PM, "main-pm")


@pytest.mark.asyncio
async def test_enabled_engine_originates_nothing_without_an_upgrade_probe(
    db_session: AsyncSession,
) -> None:
    proj = await _seed_project(db_session, "dep-a", "https://github.com/x/a.git")
    assert await get_dep_update_engine(db_session).run_cycle([proj]) == []


@pytest.mark.asyncio
async def test_disabled_is_noop(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "dep_update_enabled", False)
    proj = await _seed_project(db_session, "dep-g", "https://github.com/x/g.git")
    assert await get_dep_update_engine(db_session).run_cycle([proj]) == []
