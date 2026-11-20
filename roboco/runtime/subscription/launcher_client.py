"""Clients for the fixed-profile launcher.

``WorkerLauncher`` (executor token) is what the tool gateway uses to run a job.
``LauncherAdmin`` (admin token) registers workspaces and runs and cancels them;
only the controller holds that token. Neither carries provider credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from roboco.runtime.subscription.contracts import (
    IMPORT_TIMEOUT_SECONDS,
    ImportOptions,
    ImportOutcome,
    WorkerJob,
    WorkerResult,
)
from roboco.runtime.subscription.launcher_protocol import (
    MAX_FRAME_BYTES,
    MAX_RESPONSE_BYTES,
    LauncherAction,
    LauncherResponse,
)

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 5.0
# Longer than the launcher's hard worker timeout so its structured answer wins.
_RESPONSE_TIMEOUT_SECONDS = 700.0
_IMPORT_RESPONSE_TIMEOUT_SECONDS = IMPORT_TIMEOUT_SECONDS + 120.0


class LauncherRequestFailed(Exception):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class _LauncherConnection:
    def __init__(self, host: str, port: int, token: str) -> None:
        self._host = host
        self._port = port
        self._token = token

    async def call(
        self,
        action: LauncherAction,
        body: dict[str, Any],
        *,
        archive: bytes | None = None,
    ) -> LauncherResponse:
        frame = json.dumps(
            {"token": self._token, "action": action.value, "body": body}
        ).encode()
        if len(frame) > MAX_FRAME_BYTES:
            return LauncherResponse(ok=False, reason_code="frame_too_large")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._host, self._port, limit=MAX_RESPONSE_BYTES * 2
                ),
                _CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError):
            logger.error("Launcher unreachable at %s:%d", self._host, self._port)
            return LauncherResponse(ok=False, reason_code="launcher_unreachable")
        try:
            writer.write(frame + b"\n")
            if archive is not None:
                writer.write(archive)
            await writer.drain()
            line = await asyncio.wait_for(
                reader.readuntil(b"\n"),
                _RESPONSE_TIMEOUT_SECONDS
                if archive is None
                else _IMPORT_RESPONSE_TIMEOUT_SECONDS,
            )
            return LauncherResponse.model_validate_json(line)
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValidationError,
        ):
            return LauncherResponse(ok=False, reason_code="launcher_response_invalid")
        finally:
            writer.close()


class WorkerLauncher(_LauncherConnection):
    async def execute(self, job: WorkerJob) -> WorkerResult:
        response = await self.call(
            LauncherAction.EXECUTE, {"job": job.model_dump(mode="json")}
        )
        if response.ok and response.result is not None:
            return response.result
        return WorkerResult(
            job_id=job.job_id,
            exit_code=None,
            stdout="",
            stderr="",
            truncated=False,
            reason_code=response.reason_code or "launcher_failed",
        )


class LauncherAdmin(_LauncherConnection):
    async def _require_ok(self, action: LauncherAction, body: dict[str, Any]) -> None:
        response = await self.call(action, body)
        if not response.ok:
            raise LauncherRequestFailed(response.reason_code or "launcher_failed")

    async def register_workspace(
        self, workspace_handle: UUID, project_id: UUID
    ) -> None:
        await self._require_ok(
            LauncherAction.REGISTER_WORKSPACE,
            {"workspace_handle": str(workspace_handle), "project_id": str(project_id)},
        )

    async def import_workspace(
        self,
        workspace_handle: UUID,
        project_id: UUID,
        archive: bytes,
        options: ImportOptions | None = None,
    ) -> ImportOutcome:
        """Admit a snapshot archive into a new workspace; failure carries a reason."""
        response = await self.call(
            LauncherAction.IMPORT_WORKSPACE,
            {
                "workspace_handle": str(workspace_handle),
                "project_id": str(project_id),
                "archive_bytes": len(archive),
                "options": (options or ImportOptions()).model_dump(mode="json"),
            },
            archive=archive,
        )
        if response.import_outcome is not None:
            return response.import_outcome
        return ImportOutcome(
            ok=False, reason_code=response.reason_code or "launcher_failed"
        )

    async def register_run(
        self, run_id: UUID, project_id: UUID, workspace_handle: UUID
    ) -> None:
        await self._require_ok(
            LauncherAction.REGISTER_RUN,
            {
                "run_id": str(run_id),
                "project_id": str(project_id),
                "workspace_handle": str(workspace_handle),
            },
        )

    async def cancel_run(self, run_id: UUID) -> None:
        await self._require_ok(LauncherAction.CANCEL_RUN, {"run_id": str(run_id)})

    async def health(self) -> None:
        await self._require_ok(LauncherAction.HEALTH, {})
