"""Controller-side client for the Claude service.

Exactly three operations: status, validated run submission and cancellation. The
transport address and token come from trusted configuration, never from a task.
A transport failure is a failed run, never a retry on another path: there is no
second provider, model or authentication to fall back to.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from roboco.runtime.subscription.contracts import (
    MAX_RUN_SECONDS,
    RunRequest,
    RunResult,
    RunSpec,
    RunState,
    SubscriptionState,
    SubscriptionStatus,
)
from roboco.runtime.subscription.service_protocol import (
    MAX_FRAME_BYTES,
    MAX_RESPONSE_BYTES,
    ServiceAction,
    ServiceResponse,
)

if TYPE_CHECKING:
    from uuid import UUID

    from roboco.runtime.subscription.contracts import ToolManifest, VerbCredentials

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 5.0
_STATUS_TIMEOUT_SECONDS = 60.0
# Longer than the service's own run limit so its structured answer wins.
_RUN_TIMEOUT_SECONDS = MAX_RUN_SECONDS + 120.0


class SubscriptionClient:
    def __init__(self, host: str, port: int, token: str) -> None:
        self._host = host
        self._port = port
        self._token = token

    async def status(self) -> SubscriptionStatus:
        response = await self._call(
            ServiceAction.STATUS, {}, timeout=_STATUS_TIMEOUT_SECONDS
        )
        if response.ok and response.status is not None:
            return response.status
        return SubscriptionStatus(
            state=SubscriptionState.CONFIGURATION_ERROR,
            authenticated=False,
            auth_method=None,
            checked_at=datetime.now(UTC),
            reason_code=response.reason_code or "service_failed",
        )

    async def run(
        self,
        spec: RunSpec,
        manifest: ToolManifest | None = None,
        verb_credentials: VerbCredentials | None = None,
        session_id: str | None = None,
    ) -> RunResult:
        request = RunRequest(
            spec=spec,
            manifest=manifest,
            verb_credentials=verb_credentials,
            session_id=session_id,
        )
        response = await self._call(
            ServiceAction.RUN,
            request.model_dump(mode="json"),
            timeout=_RUN_TIMEOUT_SECONDS,
        )
        if response.ok and response.result is not None:
            return response.result
        return RunResult(
            run_id=spec.run_id,
            state=RunState.FAILED,
            text="",
            reason_code=response.reason_code or "service_failed",
            usage={},
        )

    async def cancel(self, run_id: UUID) -> None:
        await self._call(
            ServiceAction.CANCEL,
            {"run_id": str(run_id)},
            timeout=_STATUS_TIMEOUT_SECONDS,
        )

    async def _call(
        self, action: ServiceAction, body: dict[str, Any], *, timeout: float
    ) -> ServiceResponse:
        frame = json.dumps(
            {"token": self._token, "action": action.value, "body": body}
        ).encode()
        if len(frame) > MAX_FRAME_BYTES:
            return ServiceResponse(ok=False, reason_code="frame_too_large")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._host, self._port, limit=MAX_RESPONSE_BYTES * 2
                ),
                _CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError):
            logger.error("Claude service unreachable at %s:%d", self._host, self._port)
            return ServiceResponse(ok=False, reason_code="service_unreachable")
        try:
            writer.write(frame + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout)
            return ServiceResponse.model_validate_json(line)
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValidationError,
        ):
            return ServiceResponse(ok=False, reason_code="service_response_invalid")
        finally:
            writer.close()
