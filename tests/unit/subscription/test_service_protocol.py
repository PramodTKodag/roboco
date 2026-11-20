"""Service frames: strict shapes, authentication, and the client over real TCP."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from roboco.runtime.subscription.client import SubscriptionClient
from roboco.runtime.subscription.contracts import (
    RunRequest,
    RunResult,
    RunSpec,
    RunState,
    SubscriptionState,
    SubscriptionStatus,
    ToolManifest,
    WorkerOperation,
)
from roboco.runtime.subscription.service_protocol import (
    MAX_FRAME_BYTES,
    ProtocolError,
    parse_frame,
)
from roboco.runtime.subscription.service_server import ServiceFrontend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TOKEN = "t" * 40
RUN_ID = UUID(int=7)


class FakeService:
    def __init__(self) -> None:
        self.runs: list[RunRequest] = []
        self.cancelled: list[UUID] = []

    async def status(self) -> SubscriptionStatus:
        return SubscriptionStatus(
            state=SubscriptionState.READY,
            authenticated=True,
            auth_method="claude.ai",
            checked_at=datetime.now(UTC),
            reason_code=None,
        )

    async def run(self, request: RunRequest) -> RunResult:
        self.runs.append(request)
        return RunResult(
            run_id=request.spec.run_id,
            state=RunState.COMPLETED,
            text="ok",
            reason_code=None,
            usage={},
        )

    async def cancel(self, run_id: UUID) -> bool:
        self.cancelled.append(run_id)
        return True


def _spec() -> RunSpec:
    return RunSpec(
        run_id=RUN_ID,
        project_id=uuid4(),
        purpose="delivery",
        model="claude-sonnet-5-5",
        prompt="hello",
        workspace_handle=uuid4(),
    )


def _frame(action: str, body: dict, token: str = TOKEN) -> bytes:
    return json.dumps({"token": token, "action": action, "body": body}).encode()


@pytest.fixture
async def endpoint() -> AsyncIterator[tuple[str, int, FakeService]]:
    service = FakeService()
    frontend = ServiceFrontend(service, token=TOKEN)  # type: ignore[arg-type]
    server = await asyncio.start_server(
        frontend._handle_connection, "127.0.0.1", 0, limit=MAX_FRAME_BYTES + 1
    )
    host, port = server.sockets[0].getsockname()[:2]
    try:
        yield host, port, service
    finally:
        server.close()
        await server.wait_closed()


# -- frame validation -------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"argv": ["--dangerously-skip-permissions"]},
        {"env": {"ANTHROPIC_API_KEY": "x"}},
        {"cwd": "/"},
        {"cli_path": "/bin/sh"},
        {"url": "http://evil"},
        {"mount": "/:/host"},
    ],
)
def test_run_frames_cannot_carry_execution_material(extra: dict) -> None:
    body = RunRequest(spec=_spec(), manifest=None).model_dump(mode="json")
    with pytest.raises(ProtocolError, match="frame_invalid"):
        parse_frame(_frame("run", {**body, **extra}))
    spec_body = {**body, "spec": {**body["spec"], **extra}}
    with pytest.raises(ProtocolError, match="frame_invalid"):
        parse_frame(_frame("run", spec_body))


def test_a_model_outside_the_allowed_set_is_refused_at_the_frame() -> None:
    body = RunRequest(spec=_spec(), manifest=None).model_dump(mode="json")
    body["spec"]["model"] = "sonnet"
    with pytest.raises(ProtocolError, match="frame_invalid"):
        parse_frame(_frame("run", body))


@pytest.mark.parametrize("raw", [b"", b"[]", b"not json", b"{}", b'{"token": "x"}'])
def test_malformed_frames_are_rejected(raw: bytes) -> None:
    with pytest.raises(ProtocolError):
        parse_frame(raw)


def test_unknown_actions_are_rejected() -> None:
    with pytest.raises(ProtocolError, match="frame_invalid"):
        parse_frame(_frame("complete", {}))


def test_oversized_frames_are_rejected() -> None:
    with pytest.raises(ProtocolError, match="frame_too_large"):
        parse_frame(b"x" * (MAX_FRAME_BYTES + 1))


def test_short_tokens_cannot_configure_the_frontend() -> None:
    with pytest.raises(ValueError, match="at least"):
        ServiceFrontend(FakeService(), token="short")  # type: ignore[arg-type]


# -- authentication -----------------------------------------------------------


async def test_wrong_token_is_refused_before_any_service_call() -> None:
    service = FakeService()
    frontend = ServiceFrontend(service, token=TOKEN)  # type: ignore[arg-type]
    body = RunRequest(spec=_spec(), manifest=None).model_dump(mode="json")
    response = await frontend.handle_frame(_frame("run", body, token="w" * 40))
    assert (response.ok, response.reason_code) == (False, "unauthorized")
    assert service.runs == []


# -- client over real TCP -------------------------------------------------------


async def test_client_status_run_and_cancel_round_trip(
    endpoint: tuple[str, int, FakeService],
) -> None:
    host, port, service = endpoint
    client = SubscriptionClient(host, port, TOKEN)
    assert (await client.status()).state is SubscriptionState.READY
    manifest = ToolManifest(operations=(WorkerOperation.READ_FILE,))
    result = await client.run(_spec(), manifest)
    assert result.state is RunState.COMPLETED
    assert service.runs[0].manifest == manifest
    await client.cancel(RUN_ID)
    assert service.cancelled == [RUN_ID]


async def test_client_with_a_wrong_token_gets_a_failed_run_not_an_exception(
    endpoint: tuple[str, int, FakeService],
) -> None:
    host, port, service = endpoint
    result = await SubscriptionClient(host, port, "w" * 40).run(_spec())
    assert result.state is RunState.FAILED
    assert result.reason_code == "unauthorized"
    assert service.runs == []


async def test_unreachable_service_is_a_failed_run_never_a_fallback() -> None:
    client = SubscriptionClient("127.0.0.1", 1, TOKEN)
    result = await client.run(_spec())
    assert (result.state, result.reason_code) == (
        RunState.FAILED,
        "service_unreachable",
    )
    status = await client.status()
    assert status.state is SubscriptionState.CONFIGURATION_ERROR
    assert not status.authenticated
