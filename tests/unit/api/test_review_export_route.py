"""The owner-only export of accepted review patches."""

from __future__ import annotations

from http import HTTPStatus
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from roboco.api.routes.tasks import export_accepted_review_patches
from roboco.models.base import AgentRole


@pytest.mark.parametrize(
    "role",
    [AgentRole.MAIN_PM, AgentRole.CELL_PM, AgentRole.DEVELOPER, AgentRole.QA],
)
async def test_only_the_owner_may_export(role: AgentRole) -> None:
    agent = SimpleNamespace(role=role)

    with pytest.raises(HTTPException) as refused:
        await export_accepted_review_patches(uuid4(), db=None, agent=agent)  # type: ignore[arg-type]

    assert refused.value.status_code == HTTPStatus.FORBIDDEN
