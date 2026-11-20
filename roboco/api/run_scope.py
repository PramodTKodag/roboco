"""Scope gate for run-scoped model-service tokens.

The trusted model service calls the orchestrator's verb routes with a token the
orchestrator minted for one agent and one run. This gate is the only place such a
token is honored: it must be validly signed, unexpired, for a run that is still
active, and the request must target a verb route. A passing request is rewritten
to the pinned agent identity with a short-lived ordinary token, so every
downstream authorization check runs unchanged; anything else is refused here and
never reaches a route.
"""

from __future__ import annotations

import json
import re
import threading
from typing import TYPE_CHECKING, Final

import structlog

from roboco.agents_config import issue_agent_token, verify_run_token

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger()

RUN_SCOPE_PATH_PREFIXES: Final[tuple[str, ...]] = ("/api/v1/flow/", "/api/v1/do/")
# The secretary chat's own tools, by exact method and path. Confirming or rejecting
# a directive is the CEO's act and the live-chat routes are the panel's: neither is
# listed, so a run token can never reach them.
_SECRETARY_RUN_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("GET", "/api/secretary/state"),
        ("GET", "/api/secretary/tasks"),
        ("POST", "/api/secretary/directives"),
    }
)
_SECRETARY_TASK_PATH_PREFIX: Final[str] = "/api/secretary/tasks/"
# The intake interviewer's own relay calls, one session id segment each. The
# panel-facing live-chat routes (start/stream/messages/stop) are not listed.
_INTAKE_RELAY_ROUTES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("POST", re.compile(r"/api/prompter/live/[A-Za-z0-9]{1,64}/events")),
    ("GET", re.compile(r"/api/prompter/live/[A-Za-z0-9]{1,64}/search-tasks")),
)
_FORWARDED_TOKEN_TTL_SECONDS: Final[float] = 60.0
_IDENTITY_HEADERS: Final[frozenset[bytes]] = frozenset(
    {b"x-agent-id", b"x-agent-role", b"x-agent-team", b"x-agent-token"}
)


class ActiveRuns:
    """The runs the orchestrator has submitted and not yet finished."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: set[str] = set()

    def activate(self, run_id: str) -> None:
        with self._lock:
            self._runs.add(run_id)

    def deactivate(self, run_id: str) -> None:
        with self._lock:
            self._runs.discard(run_id)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._runs


ACTIVE_RUNS: Final[ActiveRuns] = ActiveRuns()


async def _refuse(send: Send, status_code: int, reason: str) -> None:
    body = json.dumps({"detail": reason}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope["headers"]:
        if key.lower() == name:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def _is_canonical_path(path: str) -> bool:
    """No dot segments and no empty middle segments.

    The path arrives percent-decoded, so ``%2e%2e`` and ``%2f`` are already
    ``..`` and ``/``. A proxy or router that normalises them afterwards would
    otherwise turn an in-scope prefix into an out-of-scope route.
    """
    segments = path.split("/")
    return "." not in segments and ".." not in segments and "" not in segments[1:-1]


def _run_token_may_reach(method: str, path: str) -> bool:
    if not _is_canonical_path(path):
        return False
    if path.startswith(RUN_SCOPE_PATH_PREFIXES):
        return True
    if any(
        method == allowed and pattern.fullmatch(path)
        for allowed, pattern in _INTAKE_RELAY_ROUTES
    ):
        return True
    if (method, path) in _SECRETARY_RUN_ROUTES:
        return True
    return (
        method == "GET"
        and path.startswith(_SECRETARY_TASK_PATH_PREFIX)
        and "/" not in path[len(_SECRETARY_TASK_PATH_PREFIX) :]
    )


class RunScopeMiddleware:
    def __init__(self, app: ASGIApp, *, active_runs: ActiveRuns = ACTIVE_RUNS) -> None:
        self._app = app
        self._active_runs = active_runs

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        token = _header(scope, b"x-agent-token")
        claims = verify_run_token(token) if token else None
        if claims is None:
            await self._app(scope, receive, send)
            return
        if not self._active_runs.is_active(claims.run_id):
            logger.warning("Run token refused: run inactive", run_id=claims.run_id)
            await _refuse(send, 401, "run is not active")
            return
        if not _run_token_may_reach(scope["method"], scope["path"]):
            logger.warning("Run token refused: out of scope", run_id=claims.run_id)
            await _refuse(send, 403, "run token is not valid for this route")
            return
        scope["headers"] = [
            *(h for h in scope["headers"] if h[0].lower() not in _IDENTITY_HEADERS),
            (b"x-agent-id", claims.agent_id.encode()),
            (b"x-agent-role", claims.role.encode()),
            (b"x-agent-team", claims.team.encode()),
            (
                b"x-agent-token",
                issue_agent_token(
                    claims.agent_id,
                    claims.role,
                    claims.team,
                    ttl_seconds=_FORWARDED_TOKEN_TTL_SECONDS,
                ).encode(),
            ),
        ]
        await self._app(scope, receive, send)
