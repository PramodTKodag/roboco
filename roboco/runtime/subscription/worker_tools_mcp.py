"""Stdio MCP bridge exposing the run's granted worker operations to the CLI.

The official CLI launches this process. It holds one run capability in its own
environment and forwards every tool call to the trusted tool gateway, which
executes it solely in the offline worker. It reads no file, runs no command and
opens no network connection itself. If the gateway denies the capability or the
grant is empty, it registers zero tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from roboco.runtime.subscription.cli import (
    GATEWAY_SOCKET_ENV,
    RUN_CAPABILITY_ENV,
    WORKER_MCP_SERVER_NAME,
)
from roboco.runtime.subscription.contracts import WorkerOperation
from roboco.runtime.subscription.gateway_socket import GatewayClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


def _render(response_ok: bool, reason: str | None, result: Any) -> str:
    if not response_ok or result is None:
        raise ToolError(f"tool request refused: {reason or 'gateway_failed'}")
    return json.dumps(
        {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
            "reason_code": result.reason_code,
        }
    )


def _tool_functions(
    client: GatewayClient,
) -> dict[WorkerOperation, Callable[..., Awaitable[str]]]:
    async def call(operation: WorkerOperation, arguments: dict[str, Any]) -> str:
        response = await client.invoke(operation.value, arguments)
        return _render(response.ok, response.reason_code, response.result)

    async def read_file(path: str, offset: int = 0) -> str:
        """Read a file from the project workspace, starting at a byte offset."""
        return await call(WorkerOperation.READ_FILE, {"path": path, "offset": offset})

    async def list_files(path: str = ".") -> str:
        """List a directory in the project workspace."""
        return await call(WorkerOperation.LIST_FILES, {"path": path})

    async def search_text(pattern: str, path: str = ".") -> str:
        """Search workspace files for a text pattern."""
        return await call(
            WorkerOperation.SEARCH_TEXT, {"pattern": pattern, "path": path}
        )

    async def write_file(path: str, content: str) -> str:
        """Write a file in the project workspace."""
        return await call(
            WorkerOperation.WRITE_FILE, {"path": path, "content": content}
        )

    async def apply_patch(patch: str) -> str:
        """Apply a unified diff to the project workspace."""
        return await call(WorkerOperation.APPLY_PATCH, {"patch": patch})

    async def run_command(command: str) -> str:
        """Run a shell command inside the offline project worker."""
        return await call(WorkerOperation.RUN_COMMAND, {"command": command})

    async def export_patch() -> str:
        """Export the workspace changes as a patch."""
        return await call(WorkerOperation.EXPORT_PATCH, {})

    async def check_conventions(files: list[str]) -> str:
        """Check the named files against the project's architectural conventions."""
        return await call(WorkerOperation.CHECK_CONVENTIONS, {"files": files})

    return {
        WorkerOperation.READ_FILE: read_file,
        WorkerOperation.LIST_FILES: list_files,
        WorkerOperation.SEARCH_TEXT: search_text,
        WorkerOperation.WRITE_FILE: write_file,
        WorkerOperation.APPLY_PATCH: apply_patch,
        WorkerOperation.RUN_COMMAND: run_command,
        WorkerOperation.EXPORT_PATCH: export_patch,
        WorkerOperation.CHECK_CONVENTIONS: check_conventions,
    }


def build_server(client: GatewayClient, granted: tuple[str, ...]) -> MCPServer:
    """A server exposing exactly the granted operations; unknown names are dropped."""
    server = MCPServer(WORKER_MCP_SERVER_NAME)
    functions = _tool_functions(client)
    for name in granted:
        try:
            operation = WorkerOperation(name)
        except ValueError:
            logger.warning("Ignoring an unknown granted operation")
            continue
        server.tool(name=operation.value)(functions[operation])
    return server


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    socket_path = os.environ.get(GATEWAY_SOCKET_ENV, "")
    capability = os.environ.get(RUN_CAPABILITY_ENV, "")
    if not socket_path or not capability:
        logger.error("Worker tools bridge has no gateway configuration: zero tools")
        build_server(GatewayClient(Path("/nonexistent"), ""), ()).run("stdio")
        return 1
    client = GatewayClient(Path(socket_path), capability)
    granted = asyncio.run(client.describe())
    build_server(client, granted).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
