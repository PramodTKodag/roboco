"""Run-scoped authority for worker tools.

The trusted service binds one capability to one registered run. A tool request
is authorized from that capability alone: the model can pass arguments but never
a run, project, workspace or job identity, and any such field fails validation.
Authorized requests become ``WorkerJob`` values executed only by the offline
worker through the launcher. Capabilities expire with the run, are revoked on
cancellation, and a missing or empty manifest on a delivery run refuses the run.
Business verbs are not authorized here: they run in their own trusted MCP servers
with an orchestrator-minted credential, and the orchestrator enforces them.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from roboco.runtime.subscription.contracts import (
    MAX_RUN_SECONDS,
    RunRequest,
    WorkerJob,
    WorkerOperation,
    WorkerPayload,
    WorkerResult,
    operations_allowed_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_PAYLOAD_ADAPTER: TypeAdapter[Any] = TypeAdapter(WorkerPayload)


class WorkerExecutor(Protocol):
    async def execute(self, job: WorkerJob) -> WorkerResult: ...


class ToolDenied(Exception):
    """A refused bind or tool request. Carries a stable reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class RunGrant:
    """What one capability may do: one run, one workspace, fixed operations."""

    __slots__ = (
        "expires_at",
        "operations",
        "project_id",
        "run_id",
        "workspace_handle",
    )

    def __init__(
        self,
        *,
        run_id: UUID,
        project_id: UUID,
        workspace_handle: UUID,
        operations: frozenset[WorkerOperation],
        expires_at: datetime,
    ) -> None:
        self.run_id = run_id
        self.project_id = project_id
        self.workspace_handle = workspace_handle
        self.operations = operations
        self.expires_at = expires_at


def _digest(capability: str) -> str:
    return hashlib.sha256(capability.encode()).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _grant_for(request: RunRequest, expires_at: datetime) -> RunGrant | None:
    """The worker grant for a run; ``None`` when it needs no worker.

    A delivery run must be granted something: worker operations, business verbs
    or both. Other purposes may use only the worker operations their purpose
    allows. Only worker operations need a project workspace.
    """
    spec = request.spec
    manifest = request.manifest
    operations = frozenset(manifest.operations) if manifest else frozenset()
    has_verbs = bool(manifest and manifest.verb_grants)
    if not operations <= operations_allowed_for(spec.purpose):
        raise ToolDenied("tools_not_allowed_for_purpose")
    if spec.purpose == "delivery" and not operations and not has_verbs:
        raise ToolDenied("manifest_missing")
    if not operations:
        return None
    if spec.project_id is None or spec.workspace_handle is None:
        raise ToolDenied("workspace_required")
    return RunGrant(
        run_id=spec.run_id,
        project_id=spec.project_id,
        workspace_handle=spec.workspace_handle,
        operations=operations,
        expires_at=expires_at,
    )


class ToolGateway:
    def __init__(
        self,
        executor: WorkerExecutor,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._executor = executor
        self._clock = clock
        self._grants: dict[str, RunGrant] = {}
        self._capability_by_run: dict[UUID, str] = {}

    def validate(self, request: RunRequest) -> None:
        """Raise ``ToolDenied`` if the request could not be bound; changes nothing."""
        _grant_for(request, self._clock())

    def bind(self, request: RunRequest) -> str | None:
        """Create the run's capability; ``None`` means the run is tool-free.

        Raises ``ToolDenied`` when a run asks for tools it may not have, or when
        a delivery run has no manifest (it never falls back to a default set).
        """
        grant = _grant_for(request, self._clock() + timedelta(seconds=MAX_RUN_SECONDS))
        if grant is None:
            return None
        if grant.run_id in self._capability_by_run:
            raise ToolDenied("run_already_bound")
        capability = secrets.token_urlsafe(32)
        self._grants[_digest(capability)] = grant
        self._capability_by_run[grant.run_id] = capability
        logger.info(
            "Tool capability bound: run=%s operations=%d",
            grant.run_id,
            len(grant.operations),
        )
        return capability

    def revoke(self, run_id: UUID) -> None:
        capability = self._capability_by_run.pop(run_id, None)
        if capability is not None:
            self._grants.pop(_digest(capability), None)
            logger.info("Tool capability revoked: run=%s", run_id)

    def operations_for(self, capability: str) -> tuple[WorkerOperation, ...]:
        grant = self._authorized(capability)
        return tuple(sorted(grant.operations, key=lambda op: op.value))

    async def invoke(
        self, capability: str, operation_name: str, arguments: dict[str, Any]
    ) -> WorkerResult:
        grant = self._authorized(capability)
        try:
            operation = WorkerOperation(operation_name)
        except ValueError:
            raise ToolDenied("unknown_operation") from None
        if operation not in grant.operations:
            logger.warning(
                "Tool denied: run=%s operation=%s not granted",
                grant.run_id,
                operation.value,
            )
            raise ToolDenied("operation_not_granted")
        if "operation" in arguments:
            raise ToolDenied("arguments_invalid")
        try:
            payload = _PAYLOAD_ADAPTER.validate_python(
                {**arguments, "operation": operation.value}
            )
            job = WorkerJob(
                job_id=uuid4(),
                run_id=grant.run_id,
                project_id=grant.project_id,
                workspace_handle=grant.workspace_handle,
                operation=operation,
                payload=payload,
            )
        except ValidationError:
            logger.info(
                "Tool arguments rejected: run=%s operation=%s",
                grant.run_id,
                operation.value,
            )
            raise ToolDenied("arguments_invalid") from None
        return await self._executor.execute(job)

    def _authorized(self, capability: str) -> RunGrant:
        grant = self._grants.get(_digest(capability))
        if grant is None:
            raise ToolDenied("capability_unknown")
        if self._clock() >= grant.expires_at:
            self.revoke(grant.run_id)
            raise ToolDenied("capability_expired")
        return grant
