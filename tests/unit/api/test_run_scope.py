"""Run-scoped tokens: pinned to one agent and run, honored only behind the gate."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import pytest
from roboco.agents_config import (
    issue_agent_token,
    issue_run_token,
    verify_agent_token,
    verify_run_token,
)
from roboco.api.run_scope import ActiveRuns, RunScopeMiddleware

if TYPE_CHECKING:
    from starlette.types import Message

AGENT = "be-dev-1"
ROLE = "backend_dev"
TEAM = "backend"
RUN = "5f0b2f4e-0000-4000-8000-000000000001"
TTL = 600.0
NOW = 1_000_000.0
FLOW_PATH = "/api/v1/flow/dev/i_am_done"


@pytest.fixture(autouse=True)
def _auth_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROBOCO_AGENT_AUTH_SECRET", "synthetic-signing-secret-for-tests")


def _token(**overrides: Any) -> str:
    values: dict[str, Any] = {
        "agent_id": AGENT,
        "role": ROLE,
        "team": TEAM,
        "run_id": RUN,
        "ttl_seconds": TTL,
    }
    values.update(overrides)
    return issue_run_token(**values)


# -- token ---------------------------------------------------------------


def test_run_token_round_trips_its_claims() -> None:
    claims = verify_run_token(_token(now=NOW), now=NOW + 1)
    assert claims is not None
    assert (claims.agent_id, claims.role, claims.team, claims.run_id) == (
        AGENT,
        ROLE,
        TEAM,
        RUN,
    )


def test_run_token_expires() -> None:
    assert verify_run_token(_token(now=NOW), now=NOW + TTL + 1) is None


def test_run_token_signed_with_another_secret_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _token()
    monkeypatch.setenv("ROBOCO_AGENT_AUTH_SECRET", "a-different-synthetic-secret")
    assert verify_run_token(token) is None


def test_a_tampered_run_token_is_rejected() -> None:
    payload, _, signature = _token().rpartition(".")
    assert verify_run_token(f"{payload}x.{signature}") is None


def test_ordinary_tokens_are_not_run_tokens() -> None:
    ordinary = issue_agent_token(AGENT, ROLE, TEAM, ttl_seconds=TTL)
    assert verify_run_token(ordinary) is None
    assert verify_run_token(issue_agent_token(AGENT, ROLE, TEAM)) is None


def test_a_run_token_never_authenticates_as_the_agents_own_token() -> None:
    assert verify_agent_token(_token(), AGENT, ROLE, TEAM) is False


def test_without_a_secret_no_run_token_is_issued_or_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ROBOCO_AGENT_AUTH_SECRET")
    assert _token() == "UNSIGNED"
    assert verify_run_token("UNSIGNED") is None


# -- scope gate ----------------------------------------------------------


class Downstream:
    def __init__(self) -> None:
        self.scope: dict[str, Any] | None = None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.scope = scope
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(
    gate: RunScopeMiddleware, path: str, headers: list[tuple[bytes, bytes]]
) -> tuple[int, list[Message]]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {"type": "http", "path": path, "headers": headers}
    await gate(scope, receive, send)
    return sent[0]["status"], sent


def _headers(token: str, **spoofed: str) -> list[tuple[bytes, bytes]]:
    pairs = {"x-agent-token": token, **spoofed}
    return [(name.encode(), value.encode()) for name, value in pairs.items()]


def _gate(*, active: bool = True) -> tuple[RunScopeMiddleware, Downstream]:
    runs = ActiveRuns()
    if active:
        runs.activate(RUN)
    downstream = Downstream()
    return RunScopeMiddleware(downstream, active_runs=runs), downstream


async def test_a_valid_run_token_is_rewritten_to_the_pinned_identity() -> None:
    gate, downstream = _gate()
    status, _ = await _call(
        gate, FLOW_PATH, _headers(_token(), **{"x-agent-role": "ceo"})
    )
    assert status == HTTPStatus.OK
    assert downstream.scope is not None
    forwarded = dict(downstream.scope["headers"])
    assert forwarded[b"x-agent-id"] == AGENT.encode()
    assert forwarded[b"x-agent-role"] == ROLE.encode()
    assert forwarded[b"x-agent-team"] == TEAM.encode()
    assert verify_agent_token(forwarded[b"x-agent-token"].decode(), AGENT, ROLE, TEAM)
    assert verify_run_token(forwarded[b"x-agent-token"].decode()) is None


async def test_do_routes_are_in_scope() -> None:
    gate, _ = _gate()
    status, _ = await _call(gate, "/api/v1/do/commit", _headers(_token()))
    assert status == HTTPStatus.OK


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/tasks",
        "/api/v1/flow",
        "/api/settings",
        "/api/v1/flowx/a",
        "/api/v1/do/../tasks",
    ],
)
async def test_out_of_scope_routes_are_refused_and_never_reach_the_app(
    path: str,
) -> None:
    gate, downstream = _gate()
    status, _ = await _call(gate, path, _headers(_token()))
    assert status == HTTPStatus.FORBIDDEN
    assert downstream.scope is None


async def test_an_inactive_run_is_refused() -> None:
    gate, downstream = _gate(active=False)
    status, _ = await _call(gate, FLOW_PATH, _headers(_token()))
    assert status == HTTPStatus.UNAUTHORIZED
    assert downstream.scope is None


async def test_a_finished_run_loses_access() -> None:
    runs = ActiveRuns()
    runs.activate(RUN)
    downstream = Downstream()
    gate = RunScopeMiddleware(downstream, active_runs=runs)
    assert (await _call(gate, FLOW_PATH, _headers(_token())))[0] == HTTPStatus.OK
    runs.deactivate(RUN)
    assert (await _call(gate, FLOW_PATH, _headers(_token())))[0] == HTTPStatus.UNAUTHORIZED


async def test_ordinary_requests_pass_through_untouched() -> None:
    gate, downstream = _gate()
    headers = _headers(issue_agent_token(AGENT, ROLE, TEAM), **{"x-agent-id": AGENT})
    status, _ = await _call(gate, "/api/v1/tasks", headers)
    assert status == HTTPStatus.OK
    assert downstream.scope is not None
    assert downstream.scope["headers"] == headers


async def test_an_invalid_run_token_is_left_for_normal_auth_to_reject() -> None:
    gate, downstream = _gate()
    payload, _, signature = _token().rpartition(".")
    headers = _headers(f"{payload}x.{signature}")
    await _call(gate, FLOW_PATH, headers)
    assert downstream.scope is not None
    assert downstream.scope["headers"] == headers
