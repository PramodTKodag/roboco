"""Model runs for delivery agents, supervised by the orchestrator.

A delivery agent is no longer a container holding a login: it is one bounded run
on the trusted model service. This module turns an agent spawn into that run
(allowed model id, role-derived tool manifest, orchestrator-minted run
credentials), keeps the run's state where the health loop can ask for it, and
maps the terminal result onto the exit codes the existing exit handling already
understands. Nothing here ever falls back to another model, provider or auth.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol, cast
from uuid import UUID, uuid4

from roboco.agents_config import issue_run_token
from roboco.foundation.policy.subscription_policy import ALLOWED_MODEL_IDS
from roboco.runtime.subscription.contracts import (
    MAX_RUN_SECONDS,
    RunResult,
    RunSpec,
    RunState,
    ToolManifest,
    VerbCredentials,
    VerbGrant,
    VerbServer,
    WorkerOperation,
)

if TYPE_CHECKING:
    from roboco.api.run_scope import ActiveRuns
    from roboco.services.gateway.role_config import RoleConfig

logger = logging.getLogger(__name__)

# Exit codes the orchestrator's exit handling already maps to "park the provider".
RATE_LIMIT_EXIT_CODE: Final[int] = 75
LOGIN_REQUIRED_EXIT_CODE: Final[int] = 78
FAILED_EXIT_CODE: Final[int] = 1
COMPLETED_EXIT_CODE: Final[int] = 0

# How long a parked subscription provider waits before its run is retried.
SUBSCRIPTION_RATE_LIMIT_RETRY_AFTER_S: Final[float] = 900.0
SUBSCRIPTION_LOGIN_RETRY_AFTER_S: Final[float] = 300.0

_EXIT_CODE_FOR_STATE: Final[dict[RunState, int]] = {
    RunState.COMPLETED: COMPLETED_EXIT_CODE,
    RunState.QUOTA_EXHAUSTED: RATE_LIMIT_EXIT_CODE,
    RunState.LOGIN_REQUIRED: LOGIN_REQUIRED_EXIT_CODE,
    RunState.BLOCKED: FAILED_EXIT_CODE,
    RunState.FAILED: FAILED_EXIT_CODE,
    RunState.CANCELLED: FAILED_EXIT_CODE,
}

# Orchestrator tier names resolve to exactly one allowed model id; anything else
# is unroutable, never substituted.
_MODEL_ID_FOR_TIER: Final[dict[str, str]] = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

_READ_OPERATIONS: Final[tuple[WorkerOperation, ...]] = (
    WorkerOperation.READ_FILE,
    WorkerOperation.LIST_FILES,
    WorkerOperation.SEARCH_TEXT,
)
_WRITE_OPERATIONS: Final[tuple[WorkerOperation, ...]] = (
    WorkerOperation.WRITE_FILE,
    WorkerOperation.APPLY_PATCH,
    WorkerOperation.RUN_COMMAND,
    WorkerOperation.EXPORT_PATCH,
    WorkerOperation.CHECK_CONVENTIONS,
)
_TOKEN_GRACE_SECONDS: Final[float] = 60.0


class UnroutableModel(Exception):
    """The configured model is not one the subscription runtime may use."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"model {model!r} is not routable on the subscription")


class WorkerToolsUnavailable(Exception):
    """A run needs offline worker tools but no launcher is configured."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id} needs worker tools; launcher not configured")


def allowed_model_id(model: str) -> str:
    """The allowed model id for an orchestrator model name, or ``UnroutableModel``."""
    candidate = _MODEL_ID_FOR_TIER.get(model, model)
    if candidate not in ALLOWED_MODEL_IDS:
        raise UnroutableModel(model)
    return candidate


def tool_manifest_for_role(role: RoleConfig, *, has_workspace: bool) -> ToolManifest:
    """The tools a role's run may use: its verbs, plus worker tools if it has files.

    Worker operations need a registered workspace; a run without one gets verbs
    only, never a tool that could not be authorized.
    """
    operations: tuple[WorkerOperation, ...] = ()
    if has_workspace:
        operations = _READ_OPERATIONS + (
            _WRITE_OPERATIONS if role.allows_write else ()
        )
    grants = tuple(
        VerbGrant(server=server, verbs=verbs)
        for server, verbs in (
            (VerbServer.FLOW, role.flow_tools),
            (VerbServer.DO, role.do_tools),
        )
        if verbs
    )
    return ToolManifest(operations=operations, verb_grants=grants)


class RunRegistrar(Protocol):
    """Registers a run with the worker launcher so worker tools can be bound."""

    async def register_run(
        self, run_id: UUID, project_id: UUID, workspace_handle: UUID
    ) -> None: ...

    async def cancel_run(self, run_id: UUID) -> None: ...


class RunClient(Protocol):
    async def run(
        self,
        spec: RunSpec,
        manifest: ToolManifest | None = None,
        verb_credentials: VerbCredentials | None = None,
        session_id: str | None = None,
    ) -> RunResult: ...

    async def cancel(self, run_id: UUID) -> None: ...


@dataclass(frozen=True)
class RunLaunch:
    """Everything a spawn or chat turn needs to become one model run."""

    agent_slug: str
    agent_id: UUID
    role: str
    team: str
    model: str
    prompt: str
    manifest: ToolManifest
    task_id: UUID | None = None
    project_id: UUID | None = None
    workspace_handle: UUID | None = None
    purpose: Literal["delivery", "intake", "secretary"] = "delivery"
    session_id: str | None = None


@dataclass(frozen=True)
class _RunInputs:
    manifest: ToolManifest
    credentials: VerbCredentials
    session_id: str | None


class _Run:
    __slots__ = ("result", "run_id", "task")

    def __init__(self, run_id: UUID, task: asyncio.Task[RunResult]) -> None:
        self.run_id = run_id
        self.task = task
        self.result: RunResult | None = None


class RunSupervisor:
    """Starts delivery runs and answers the health loop about them."""

    def __init__(
        self,
        client: RunClient,
        registrar: RunRegistrar | None,
        active_runs: ActiveRuns,
    ) -> None:
        self._client = client
        self._registrar = registrar
        self._active_runs = active_runs
        self._runs: dict[str, _Run] = {}

    async def start(self, launch: RunLaunch) -> str:
        """Begin a run for the agent; returns its handle (the run id).

        Refuses (raises) rather than degrade: an unroutable model, or a worker
        registration that fails, never starts a run with fewer guarantees.
        """
        model = allowed_model_id(launch.model)
        run_id = uuid4()
        spec = RunSpec(
            run_id=run_id,
            project_id=launch.project_id,
            task_id=launch.task_id,
            agent_id=launch.agent_id,
            purpose=launch.purpose,
            model=model,
            prompt=launch.prompt,
            workspace_handle=launch.workspace_handle,
        )
        token = issue_run_token(
            str(launch.agent_id),
            launch.role,
            launch.team,
            str(run_id),
            ttl_seconds=MAX_RUN_SECONDS + _TOKEN_GRACE_SECONDS,
        )
        credentials = VerbCredentials(
            agent_id=launch.agent_id, role=launch.role, token=token
        )
        registered = (
            launch.project_id is not None and launch.workspace_handle is not None
        )
        if registered:
            if self._registrar is None:
                raise WorkerToolsUnavailable(str(run_id))
            await self._registrar.register_run(
                run_id,
                cast("UUID", launch.project_id),
                cast("UUID", launch.workspace_handle),
            )
        self._active_runs.activate(str(run_id))
        task = asyncio.create_task(
            self._drive(
                launch.agent_slug,
                spec,
                _RunInputs(launch.manifest, credentials, launch.session_id),
                registered,
            ),
            name=f"model-run-{launch.agent_slug}",
        )
        self._runs[launch.agent_slug] = _Run(run_id, task)
        logger.info(
            "Model run started: agent=%s run=%s model=%s",
            launch.agent_slug,
            run_id,
            model,
        )
        return str(run_id)

    async def _drive(
        self,
        agent_slug: str,
        spec: RunSpec,
        inputs: _RunInputs,
        registered: bool,
    ) -> RunResult:
        try:
            result = await self._client.run(
                spec, inputs.manifest, inputs.credentials, inputs.session_id
            )
        except Exception:
            logger.exception(
                "Model run crashed: agent=%s run=%s", agent_slug, spec.run_id
            )
            result = RunResult(
                run_id=spec.run_id,
                state=RunState.FAILED,
                text="",
                reason_code="run_supervisor_failed",
                usage={},
            )
        finally:
            self._active_runs.deactivate(str(spec.run_id))
            if registered:
                await self._release_worker_registration(spec.run_id)
        record = self._runs.get(agent_slug)
        if record is not None and record.run_id == spec.run_id:
            record.result = result
        logger.info(
            "Model run ended: agent=%s run=%s state=%s reason=%s",
            agent_slug,
            spec.run_id,
            result.state.value,
            result.reason_code,
        )
        return result

    async def _release_worker_registration(self, run_id: UUID) -> None:
        if self._registrar is None:
            return
        try:
            await self._registrar.cancel_run(run_id)
        except Exception:
            logger.exception("Worker run release failed: run=%s", run_id)

    async def result(self, agent_slug: str) -> RunResult | None:
        """Wait for the agent's current run to end; ``None`` if it has none."""
        record = self._runs.get(agent_slug)
        if record is None:
            return None
        try:
            return await record.task
        except asyncio.CancelledError:
            return None

    def owns(self, agent_slug: str) -> bool:
        return agent_slug in self._runs

    def state(self, agent_slug: str) -> tuple[bool, int | None]:
        """``(is_running, exit_code)`` in the shape the health loop expects."""
        record = self._runs.get(agent_slug)
        if record is None:
            return False, None
        if not record.task.done():
            return True, None
        result = record.result
        if result is None:
            return False, FAILED_EXIT_CODE
        return False, _EXIT_CODE_FOR_STATE[result.state]

    async def cancel(self, agent_slug: str) -> None:
        """Stop the agent's run and revoke its credentials; idempotent."""
        record = self._runs.get(agent_slug)
        if record is None:
            return
        self._active_runs.deactivate(str(record.run_id))
        if not record.task.done():
            await self._client.cancel(record.run_id)
            record.task.cancel()
        logger.info("Model run cancelled: agent=%s run=%s", agent_slug, record.run_id)

    def forget(self, agent_slug: str) -> None:
        """Drop the finished run so the agent can start a new one."""
        self._runs.pop(agent_slug, None)
