"""Host allowlist and cross-origin refusal for cookie-authenticated requests."""

from __future__ import annotations

from typing import Any

import pytest

from roboco.api.access_guard import LocalAccessGuardMiddleware
from roboco.api.auth.backend import SESSION_COOKIE_NAME

ORIGIN = "http://localhost:3000"
HOST = "localhost:3000"
COOKIE = f"{SESSION_COOKIE_NAME}=synthetic-session-value"


class _Recorder:
    def __init__(self) -> None:
        self.reached = False
        self.sent: list[dict[str, Any]] = []

    async def app(self, scope: Any, receive: Any, send: Any) -> None:
        self.reached = True

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


async def _receive() -> dict[str, Any]:
    return {"type": "http.request"}


def _scope(
    kind: str = "http",
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    merged = {"host": HOST, **(headers or {})}
    scope: dict[str, Any] = {
        "type": kind,
        "headers": [(k.encode(), v.encode()) for k, v in merged.items()],
    }
    if kind == "http":
        scope["method"] = method
    return scope


async def _run(scope: dict[str, Any]) -> _Recorder:
    recorder = _Recorder()
    guard = LocalAccessGuardMiddleware(
        recorder.app,
        allowed_hosts=[HOST, "orchestrator:8000"],
        allowed_origins=[ORIGIN],
    )
    await guard(scope, _receive, recorder.send)
    return recorder


def _status(recorder: _Recorder) -> int:
    return int(recorder.sent[0]["status"])


@pytest.mark.parametrize(
    "host", ["evil.example", "localhost", "localhost:3001", "localhost:3000.evil.test", ""]
)
async def test_an_unlisted_host_is_refused(host: str) -> None:
    recorder = await _run(_scope(headers={"host": host}))
    assert not recorder.reached
    assert _status(recorder) == 400


async def test_a_listed_internal_host_is_served_without_a_cookie() -> None:
    recorder = await _run(_scope(headers={"host": "orchestrator:8000"}))
    assert recorder.reached


async def test_a_state_change_with_the_session_cookie_needs_an_allowed_origin() -> None:
    recorder = await _run(
        _scope(method="POST", headers={"cookie": COOKIE, "origin": ORIGIN})
    )
    assert recorder.reached


@pytest.mark.parametrize(
    "origin", [None, "null", "http://evil.example", "http://localhost:5174", ORIGIN + ".evil"]
)
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_a_cross_origin_state_change_with_the_cookie_is_refused(
    method: str, origin: str | None
) -> None:
    headers = {"cookie": COOKIE}
    if origin is not None:
        headers["origin"] = origin
    recorder = await _run(_scope(method=method, headers=headers))
    assert not recorder.reached
    assert _status(recorder) == 403


async def test_a_state_change_without_the_cookie_is_not_a_browser_session() -> None:
    recorder = await _run(_scope(method="POST", headers={"x-agent-token": "signed"}))
    assert recorder.reached


async def test_a_cookie_read_from_another_origin_is_not_a_state_change() -> None:
    recorder = await _run(
        _scope(headers={"cookie": COOKIE, "origin": "http://evil.example"})
    )
    assert recorder.reached


async def test_an_unrelated_cookie_does_not_trigger_the_origin_rule() -> None:
    recorder = await _run(_scope(method="POST", headers={"cookie": "theme=dark"}))
    assert recorder.reached


async def test_a_websocket_with_the_cookie_needs_an_allowed_origin() -> None:
    allowed = await _run(_scope("websocket", headers={"cookie": COOKIE, "origin": ORIGIN}))
    assert allowed.reached
    foreign = await _run(
        _scope("websocket", headers={"cookie": COOKIE, "origin": "http://evil.example"})
    )
    assert not foreign.reached
    assert foreign.sent == [{"type": "websocket.close", "code": 1008}]


async def test_a_websocket_to_an_unlisted_host_is_closed() -> None:
    recorder = await _run(_scope("websocket", headers={"host": "evil.example"}))
    assert not recorder.reached
    assert recorder.sent == [{"type": "websocket.close", "code": 1008}]


async def test_refusals_do_not_echo_request_content() -> None:
    recorder = await _run(
        _scope(method="POST", headers={"cookie": COOKIE, "origin": "http://evil.example"})
    )
    body = recorder.sent[1]["body"].decode()
    assert "synthetic-session-value" not in body
    assert "evil.example" not in body
