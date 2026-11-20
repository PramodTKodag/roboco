"""Gateway socket: authority comes from the capability, never from arguments."""

from __future__ import annotations

import json
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from roboco.runtime.subscription.contracts import (
    RunRequest,
    RunSpec,
    ToolManifest,
    WorkerJob,
    WorkerOperation,
    WorkerResult,
)
from roboco.runtime.subscription.gateway_socket import GatewayClient, GatewayServer
from roboco.runtime.subscription.tool_gateway import ToolGateway

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class Executor:
    def __init__(self) -> None:
        self.jobs: list[WorkerJob] = []

    async def execute(self, job: WorkerJob) -> WorkerResult:
        self.jobs.append(job)
        return WorkerResult(
            job_id=job.job_id,
            exit_code=0,
            stdout="from-worker",
            stderr="",
            truncated=False,
            reason_code=None,
        )


def _request(*operations: WorkerOperation) -> RunRequest:
    return RunRequest(
        spec=RunSpec(
            run_id=uuid4(),
            project_id=uuid4(),
            purpose="delivery",
            model="claude-sonnet-5-5",
            prompt="p",
            workspace_handle=uuid4(),
        ),
        manifest=ToolManifest(operations=operations),
    )


_OWNER_ONLY_MODE = 0o600


@pytest.fixture
async def stack() -> AsyncIterator[tuple[ToolGateway, Executor, Path]]:
    # A unix socket path is limited to ~104 bytes, so pytest's tmp_path is too long.
    with tempfile.TemporaryDirectory(prefix="gw") as short_dir:
        executor = Executor()
        gateway = ToolGateway(executor)
        server = GatewayServer(gateway)
        path = Path(short_dir) / "gw" / "gateway.sock"
        await server.start(path)
        try:
            yield gateway, executor, path
        finally:
            await server.stop()


async def test_socket_is_owner_only(stack: tuple[ToolGateway, Executor, Path]) -> None:
    _, _, path = stack
    assert stat.S_IMODE(path.stat().st_mode) == _OWNER_ONLY_MODE


async def test_describe_and_invoke_use_the_bound_run(
    stack: tuple[ToolGateway, Executor, Path],
) -> None:
    gateway, executor, path = stack
    request = _request(WorkerOperation.READ_FILE, WorkerOperation.LIST_FILES)
    capability = gateway.bind(request)
    assert capability is not None
    client = GatewayClient(path, capability)
    assert set(await client.describe()) == {"read_file", "list_files"}
    response = await client.invoke("read_file", {"path": "a.py"})
    assert response.ok
    assert response.result is not None
    assert response.result.stdout == "from-worker"
    assert executor.jobs[0].run_id == request.spec.run_id


async def test_unknown_capability_describes_zero_tools_and_cannot_invoke(
    stack: tuple[ToolGateway, Executor, Path],
) -> None:
    _, executor, path = stack
    client = GatewayClient(path, "not-a-capability")
    assert await client.describe() == ()
    response = await client.invoke("read_file", {"path": "a.py"})
    assert (response.ok, response.reason_code) == (False, "capability_unknown")
    assert executor.jobs == []


async def test_model_arguments_cannot_change_the_run(
    stack: tuple[ToolGateway, Executor, Path],
) -> None:
    gateway, executor, path = stack
    capability = gateway.bind(_request(WorkerOperation.READ_FILE))
    assert capability is not None
    response = await GatewayClient(path, capability).invoke(
        "read_file", {"path": "a.py", "run_id": str(uuid4())}
    )
    assert (response.ok, response.reason_code) == (False, "arguments_invalid")
    assert executor.jobs == []


async def test_revoked_capability_is_denied_on_the_next_call(
    stack: tuple[ToolGateway, Executor, Path],
) -> None:
    gateway, _, path = stack
    request = _request(WorkerOperation.READ_FILE)
    capability = gateway.bind(request)
    assert capability is not None
    client = GatewayClient(path, capability)
    gateway.revoke(request.spec.run_id)
    assert await client.describe() == ()
    assert (await client.invoke("read_file", {"path": "a"})).reason_code == (
        "capability_unknown"
    )


async def test_responses_never_echo_the_capability(
    stack: tuple[ToolGateway, Executor, Path],
) -> None:
    _, _, path = stack
    secret = "synthetic-capability-sentinel-44f1"
    response = await GatewayClient(path, secret).invoke("read_file", {})
    assert secret not in response.model_dump_json()


async def test_malformed_frames_get_a_stable_reason(
    stack: tuple[ToolGateway, Executor, Path],
) -> None:
    gateway, _, _ = stack
    server = GatewayServer(gateway)
    for raw in (b"{}", b"[]", b"nope", json.dumps({"capability": "x"}).encode()):
        assert (await server.respond(raw)).reason_code == "frame_invalid"


async def test_unreachable_gateway_means_zero_tools(tmp_path: Path) -> None:
    client = GatewayClient(tmp_path / "missing.sock", "cap")
    assert await client.describe() == ()
    assert (await client.invoke("read_file", {})).reason_code == "gateway_unreachable"
