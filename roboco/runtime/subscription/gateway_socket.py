"""Unix-socket transport between the worker-tools MCP bridge and the tool gateway.

The bridge is a child of the official CLI and sends its run capability on every
frame. The server resolves authority from that capability alone and never from a
field the model could influence. Responses carry the worker result or a stable
reason code, never the capability, argv or any environment value.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, ValidationError

from roboco.runtime.subscription.contracts import MAX_FILE_BYTES, WorkerResult
from roboco.runtime.subscription.tool_gateway import ToolDenied, ToolGateway

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

MAX_GATEWAY_FRAME_BYTES = 2 * MAX_FILE_BYTES + 64 * 1024
_FRAME_READ_TIMEOUT_SECONDS = 30.0
_CALL_TIMEOUT_SECONDS = 700.0
_SOCKET_UMASK = 0o177  # owner read/write only


class GatewayRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    capability: str
    action: str
    operation: str | None = None
    arguments: dict[str, Any] = {}


class GatewayResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    reason_code: str | None = None
    operations: tuple[str, ...] = ()
    result: WorkerResult | None = None


class GatewayServer:
    def __init__(self, gateway: ToolGateway) -> None:
        self._gateway = gateway
        self._server: asyncio.Server | None = None

    async def start(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        # The socket is created owner-only; there is no window where it is wider.
        previous = os.umask(_SOCKET_UMASK)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, path=str(path), limit=MAX_GATEWAY_FRAME_BYTES
            )
        finally:
            os.umask(previous)
        logger.info("Tool gateway listening on a unix socket")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\n"), _FRAME_READ_TIMEOUT_SECONDS
                )
                response = await self.respond(raw.rstrip(b"\n"))
            except (
                asyncio.LimitOverrunError,
                asyncio.IncompleteReadError,
                TimeoutError,
            ):
                response = GatewayResponse(ok=False, reason_code="frame_unreadable")
            writer.write(response.model_dump_json().encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    async def respond(self, raw: bytes) -> GatewayResponse:
        try:
            request = GatewayRequest.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            return GatewayResponse(ok=False, reason_code="frame_invalid")
        try:
            if request.action == "describe":
                operations = self._gateway.operations_for(request.capability)
                return GatewayResponse(
                    ok=True, operations=tuple(op.value for op in operations)
                )
            if request.action == "invoke" and request.operation is not None:
                result = await self._gateway.invoke(
                    request.capability, request.operation, request.arguments
                )
                return GatewayResponse(ok=True, result=result)
        except ToolDenied as denied:
            return GatewayResponse(ok=False, reason_code=denied.reason_code)
        return GatewayResponse(ok=False, reason_code="frame_invalid")


class GatewayClient:
    """Used by the bridge. Holds its run capability and nothing else."""

    def __init__(self, socket_path: Path, capability: str) -> None:
        self._socket_path = socket_path
        self._capability = capability

    async def _call(self, body: dict[str, Any]) -> GatewayResponse:
        frame = json.dumps({"capability": self._capability, **body}).encode()
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self._socket_path), limit=MAX_GATEWAY_FRAME_BYTES
            )
        except OSError:
            return GatewayResponse(ok=False, reason_code="gateway_unreachable")
        try:
            writer.write(frame + b"\n")
            await writer.drain()
            line = await asyncio.wait_for(
                reader.readuntil(b"\n"), _CALL_TIMEOUT_SECONDS
            )
            return GatewayResponse.model_validate_json(line)
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            ValidationError,
        ):
            return GatewayResponse(ok=False, reason_code="gateway_response_invalid")
        finally:
            writer.close()

    async def describe(self) -> tuple[str, ...]:
        """Granted operation names; empty when denied, so a bad grant means no tools."""
        response = await self._call({"action": "describe"})
        return response.operations if response.ok else ()

    async def invoke(
        self, operation: str, arguments: dict[str, Any]
    ) -> GatewayResponse:
        return await self._call(
            {"action": "invoke", "operation": operation, "arguments": arguments}
        )
