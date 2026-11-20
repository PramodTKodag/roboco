"""Server-side launcher policy: registries, job authorization, fixed Docker profile.

Pure and stdlib-only apart from the contracts. Mount sources and container names
derive from registered UUIDs, never from request strings, and the Docker argv is
assembled from constants plus those derived values.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from roboco.runtime.subscription.contracts import COMMAND_TIMEOUT_SECONDS
from roboco.runtime.subscription.launcher_protocol import LauncherAction

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from roboco.runtime.subscription.contracts import WorkerJob

logger = logging.getLogger(__name__)

WORKER_LABEL = "roboco.personal.worker"
RUN_LABEL = "roboco.personal.run"
JOB_LABEL = "roboco.personal.job"
WORKSPACE_MOUNT = "/workspace"
VOLUME_PREFIX = "roboco_ws_"
MIN_TOKEN_LENGTH = 24
ALLOWED_WORKER_RUNTIMES = frozenset({"runc", "runsc"})


class JobRejection(Exception):
    """A request refused by launcher policy. Carries a stable reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class LauncherConfig:
    """Trusted launcher configuration. Never constructed from request data."""

    image: str
    runtime: str = "runc"
    user: str = "10001:10001"
    cpus: str = "2"
    memory: str = "2g"
    pids_limit: int = 256
    tmpfs_size: str = "256m"
    command_timeout_seconds: int = COMMAND_TIMEOUT_SECONDS
    hard_kill_grace_seconds: int = 30

    def __post_init__(self) -> None:
        if self.runtime not in ALLOWED_WORKER_RUNTIMES:
            raise ValueError("worker runtime is not allowed")
        if not self.image or any(c.isspace() for c in self.image):
            raise ValueError("worker image must be a single pinned reference")


@dataclass(frozen=True)
class AuthorizedJob:
    job: WorkerJob
    volume_name: str
    container_name: str


@dataclass
class _RunRecord:
    project_id: UUID
    workspace_handle: UUID
    expires_at: datetime
    cancelled: bool = False
    seen_jobs: set[UUID] = field(default_factory=set)


class LauncherRegistry:
    """Server-side ownership state: workspaces, runs, replay and cancellation."""

    def __init__(self, *, run_ttl: timedelta, max_runs: int = 256) -> None:
        self._run_ttl = run_ttl
        self._max_runs = max_runs
        self._workspaces: dict[UUID, UUID] = {}
        self._runs: dict[UUID, _RunRecord] = {}

    def register_workspace(self, workspace_handle: UUID, project_id: UUID) -> None:
        owner = self._workspaces.setdefault(workspace_handle, project_id)
        if owner != project_id:
            raise JobRejection("workspace_owned_by_other_project")

    def register_run(
        self,
        run_id: UUID,
        project_id: UUID,
        workspace_handle: UUID,
        now: datetime,
    ) -> None:
        self._purge_expired(now)
        if run_id in self._runs:
            raise JobRejection("run_already_registered")
        if self._workspaces.get(workspace_handle) is None:
            raise JobRejection("workspace_unknown")
        if self._workspaces[workspace_handle] != project_id:
            raise JobRejection("project_mismatch")
        if len(self._runs) >= self._max_runs:
            raise JobRejection("too_many_runs")
        self._runs[run_id] = _RunRecord(
            project_id=project_id,
            workspace_handle=workspace_handle,
            expires_at=now + self._run_ttl,
        )

    def cancel_run(self, run_id: UUID) -> None:
        record = self._runs.get(run_id)
        if record is not None:
            record.cancelled = True

    def is_cancelled(self, run_id: UUID) -> bool:
        record = self._runs.get(run_id)
        return record is not None and record.cancelled

    def authorize(self, job: WorkerJob, now: datetime) -> AuthorizedJob:
        record = self._runs.get(job.run_id)
        if record is None:
            raise JobRejection("run_unknown")
        if record.cancelled:
            raise JobRejection("run_cancelled")
        if now >= record.expires_at:
            raise JobRejection("run_expired")
        if job.project_id != record.project_id:
            raise JobRejection("project_mismatch")
        if job.workspace_handle != record.workspace_handle:
            raise JobRejection("workspace_mismatch")
        if self._workspaces.get(job.workspace_handle) != record.project_id:
            raise JobRejection("workspace_mismatch")
        if job.job_id in record.seen_jobs:
            raise JobRejection("job_replayed")
        record.seen_jobs.add(job.job_id)
        return AuthorizedJob(
            job=job,
            volume_name=f"{VOLUME_PREFIX}{job.workspace_handle.hex}",
            container_name=f"roboco-worker-{uuid4().hex[:16]}",
        )

    def _purge_expired(self, now: datetime) -> None:
        expired = [rid for rid, rec in self._runs.items() if now >= rec.expires_at]
        for run_id in expired:
            del self._runs[run_id]


class ClientAuthenticator:
    """Two distinct bearer tokens, each granting only its own actions."""

    def __init__(self, *, admin_token: str, executor_token: str) -> None:
        if min(len(admin_token), len(executor_token)) < MIN_TOKEN_LENGTH:
            raise ValueError(f"tokens must be at least {MIN_TOKEN_LENGTH} characters")
        if hmac.compare_digest(admin_token, executor_token):
            raise ValueError("admin and executor tokens must be distinct")
        self._grants: tuple[tuple[str, frozenset[LauncherAction]], ...] = (
            (
                admin_token,
                frozenset(
                    {
                        LauncherAction.HEALTH,
                        LauncherAction.REGISTER_WORKSPACE,
                        LauncherAction.REGISTER_RUN,
                        LauncherAction.CANCEL_RUN,
                    }
                ),
            ),
            (
                executor_token,
                frozenset({LauncherAction.HEALTH, LauncherAction.EXECUTE}),
            ),
        )

    def allows(self, token: str, action: LauncherAction) -> bool:
        allowed = False
        for known, actions in self._grants:
            if hmac.compare_digest(token.encode(), known.encode()):
                allowed = allowed or action in actions
        return allowed


def build_worker_run_argv(
    config: LauncherConfig, authorized: AuthorizedJob
) -> list[str]:
    """Docker argv for one disposable worker: constants plus derived identifiers."""
    job = authorized.job
    return [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--init",
        "--name",
        authorized.container_name,
        "--label",
        f"{WORKER_LABEL}=1",
        "--label",
        f"{RUN_LABEL}={job.run_id}",
        "--label",
        f"{JOB_LABEL}={job.job_id}",
        "--runtime",
        config.runtime,
        "--network",
        "none",
        "--ipc",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(config.pids_limit),
        "--memory",
        config.memory,
        "--memory-swap",
        config.memory,
        "--cpus",
        config.cpus,
        "--user",
        config.user,
        "--tmpfs",
        f"/tmp:rw,nosuid,nodev,size={config.tmpfs_size}",  # nosec B108
        "--mount",
        f"type=volume,src={authorized.volume_name},dst={WORKSPACE_MOUNT}",
        config.image,
    ]
