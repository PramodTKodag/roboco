"""The stdio bridge registers exactly the granted tools, or none."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from roboco.runtime.subscription.contracts import (
    CONTROLLER_ONLY_OPERATIONS,
    WorkerOperation,
    WorkerResult,
)
from roboco.runtime.subscription.gateway_socket import GatewayResponse
from roboco.runtime.subscription.worker_tools_mcp import build_server


class FakeClient:
    def __init__(self, response: GatewayResponse | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.response = response or GatewayResponse(
            ok=True,
            result=WorkerResult(
                job_id=uuid4(),
                exit_code=0,
                stdout="out",
                stderr="",
                truncated=False,
                reason_code=None,
            ),
        )

    async def invoke(
        self, operation: str, arguments: dict[str, Any]
    ) -> GatewayResponse:
        self.calls.append((operation, arguments))
        return self.response


async def _names(server: Any) -> set[str]:
    return {tool.name for tool in await server.list_tools()}


async def test_exactly_the_granted_operations_are_registered() -> None:
    server = build_server(FakeClient(), ("read_file", "run_command"))  # type: ignore[arg-type]
    assert await _names(server) == {"read_file", "run_command"}


@pytest.mark.parametrize("granted", [(), ("delete_everything",), ("",), ("READ_FILE",)])
async def test_nothing_or_only_unknown_names_register_zero_tools(
    granted: tuple[str, ...],
) -> None:
    server = build_server(FakeClient(), granted)  # type: ignore[arg-type]
    assert await _names(server) == set()


async def test_every_operation_has_a_bridge_function() -> None:
    names = tuple(
        op.value for op in WorkerOperation if op not in CONTROLLER_ONLY_OPERATIONS
    )
    server = build_server(FakeClient(), names)  # type: ignore[arg-type]
    assert await _names(server) == set(names)


async def test_a_tool_call_is_forwarded_with_only_typed_arguments() -> None:
    client = FakeClient()
    server = build_server(client, ("run_command",))  # type: ignore[arg-type]
    result = await server.call_tool("run_command", {"command": "make test"})
    assert client.calls == [("run_command", {"command": "make test"})]
    text = json.dumps(result, default=str)
    assert "out" in text


async def test_a_refused_request_surfaces_as_a_tool_error() -> None:
    client = FakeClient(GatewayResponse(ok=False, reason_code="operation_not_granted"))
    server = build_server(client, ("run_command",))  # type: ignore[arg-type]
    with pytest.raises(ToolError, match="operation_not_granted"):
        await server.call_tool("run_command", {"command": "id"})
