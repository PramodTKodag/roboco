"""Authenticated TCP front end of the Claude service."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from typing import TYPE_CHECKING

from roboco.runtime.subscription.service_protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    RunFrame,
    ServiceResponse,
    StatusFrame,
    encode_response,
    parse_frame,
)

if TYPE_CHECKING:
    from roboco.runtime.subscription.service import ClaudeService

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
_FRAME_READ_TIMEOUT_SECONDS = 30.0


class ServiceFrontend:
    def __init__(self, service: ClaudeService, *, token: str) -> None:
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"token must be at least {MIN_TOKEN_LENGTH} characters")
        self._service = service
        self._token = token

    async def handle_frame(self, raw: bytes) -> ServiceResponse:
        try:
            frame = parse_frame(raw)
        except ProtocolError as exc:
            return ServiceResponse(ok=False, reason_code=str(exc))
        if not hmac.compare_digest(frame.token.encode(), self._token.encode()):
            logger.warning("Service request refused: unauthorized")
            return ServiceResponse(ok=False, reason_code="unauthorized")
        if isinstance(frame, StatusFrame):
            return ServiceResponse(ok=True, status=await self._service.status())
        if isinstance(frame, RunFrame):
            return ServiceResponse(ok=True, result=await self._service.run(frame.body))
        cancelled = await self._service.cancel(frame.body.run_id)
        return ServiceResponse(ok=True, cancelled=cancelled)

    async def serve(self, host: str, port: int) -> None:
        server = await asyncio.start_server(
            self._handle_connection, host, port, limit=MAX_FRAME_BYTES + 1
        )
        logger.info("Claude service listening on %s:%d", host, port)
        async with server:
            await server.serve_forever()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\n"), _FRAME_READ_TIMEOUT_SECONDS
                )
                response = await self.handle_frame(raw.rstrip(b"\n"))
            except (
                asyncio.LimitOverrunError,
                asyncio.IncompleteReadError,
                TimeoutError,
            ):
                response = ServiceResponse(ok=False, reason_code="frame_unreadable")
            writer.write(encode_response(response))
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()
