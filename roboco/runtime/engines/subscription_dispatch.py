"""Builds the delivery-run supervisor and the run for an agent spawn.

Configuration is read from settings and fails closed: with no service address or
token there is no run supervisor, and a spawn is refused instead of falling back
to any other runtime. Token files are read here, never from the environment dump.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from roboco.agents_config import get_agent_role, get_agent_team
from roboco.api.run_scope import ACTIVE_RUNS
from roboco.runtime.engines.subscription_runs import (
    RunLaunch,
    RunSupervisor,
    tool_manifest_for_role,
)
from roboco.runtime.subscription.client import SubscriptionClient
from roboco.runtime.subscription.launcher_client import LauncherAdmin
from roboco.seeds.initial_data import AGENT_UUIDS
from roboco.services.gateway.role_config import ROLE_CONFIGS, get_role_config

if TYPE_CHECKING:
    from roboco.config import Settings
    from roboco.models.runtime import AgentConfig

logger = logging.getLogger(__name__)

_MAX_TOKEN_FILE_BYTES = 4096


class SubscriptionNotConfigured(RuntimeError):
    """Model runs are not configured; nothing else may run the agent instead."""


class WorkspaceNotLinked(RuntimeError):
    """The project has no imported offline workspace, so a run cannot touch files."""


@dataclass(frozen=True)
class ProjectBinding:
    project_id: UUID
    workspace_handle: UUID | None


def read_token_file(path: str, *, what: str) -> str:
    """The stripped token in ``path``; refuses a missing, empty or oversized file."""
    if not path:
        raise SubscriptionNotConfigured(f"{what} token file is not configured")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise SubscriptionNotConfigured(f"{what} token file is unreadable") from None
    token = raw.decode("utf-8", errors="strict").strip()
    if not token or len(raw) > _MAX_TOKEN_FILE_BYTES:
        raise SubscriptionNotConfigured(f"{what} token file is empty or oversized")
    return token


def build_run_supervisor(config: Settings) -> RunSupervisor:
    """The supervisor for the configured service; raises when it is not configured."""
    if not config.claude_service_host:
        raise SubscriptionNotConfigured("claude service host is not configured")
    client = SubscriptionClient(
        config.claude_service_host,
        config.claude_service_port,
        read_token_file(config.claude_service_token_file, what="claude service"),
    )
    registrar: LauncherAdmin | None = None
    if config.launcher_host:
        registrar = LauncherAdmin(
            config.launcher_host,
            config.launcher_port,
            read_token_file(config.launcher_admin_token_file, what="launcher admin"),
        )
    return RunSupervisor(client, registrar, ACTIVE_RUNS)


async def resolve_project_binding(project_slug: str | None) -> ProjectBinding | None:
    """The project's id and linked offline workspace; None for a project-less agent.

    An unknown slug is refused: an agent never runs against a project that does
    not exist.
    """
    if not project_slug:
        return None
    from roboco.db.base import get_session_factory
    from roboco.services.project import get_project_service

    async with get_session_factory()() as session:
        project = await get_project_service(session).get_by_slug(project_slug)
        if project is None:
            raise WorkspaceNotLinked(f"project {project_slug!r} does not exist")
        return ProjectBinding(
            project_id=UUID(str(project.id)),
            workspace_handle=project.offline_workspace_handle,
        )


async def _read_text(path: Path | None) -> str:
    if path is None:
        return ""
    return await asyncio.to_thread(path.read_text, "utf-8")


async def compose_run_prompt(config: AgentConfig, initial_prompt: str) -> str:
    """Blueprint, then briefing, then the spawn prompt, as one stdin prompt."""
    parts = [
        await _read_text(config.blueprint_path),
        await _read_text(config.briefing_path),
        initial_prompt,
    ]
    return "\n\n".join(part for part in parts if part.strip())


async def build_run_launch(
    config: AgentConfig,
    initial_prompt: str,
    binding: ProjectBinding | None,
) -> RunLaunch:
    """The run an agent spawn becomes; refuses a project without a workspace."""
    slug = config.agent_id
    role = get_agent_role(slug)
    if role not in ROLE_CONFIGS:
        raise SubscriptionNotConfigured(f"agent {slug!r} has no gateway role")
    team = get_agent_team(slug)
    agent_uuid = AGENT_UUIDS.get(slug)
    if team is None or agent_uuid is None:
        raise SubscriptionNotConfigured(f"agent {slug!r} has no role/team identity")
    has_workspace = binding is not None and binding.workspace_handle is not None
    if binding is not None and not has_workspace:
        raise WorkspaceNotLinked(
            f"project of agent {slug!r} has no imported offline workspace"
        )
    return RunLaunch(
        agent_slug=slug,
        agent_id=UUID(agent_uuid),
        role=role,
        team=team,
        model=config.model,
        prompt=await compose_run_prompt(config, initial_prompt),
        manifest=tool_manifest_for_role(
            get_role_config(role), has_workspace=has_workspace
        ),
        project_id=binding.project_id if binding else None,
        workspace_handle=binding.workspace_handle if binding else None,
    )
