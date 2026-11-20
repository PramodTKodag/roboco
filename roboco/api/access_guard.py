"""Local-owner access guard: Host allowlist, cross-origin refusal for cookie sessions.

Armed with cloud auth, where a browser session cookie is the owner's credential.
A cookie is attached by the browser to any request to this host, whichever page
caused it, and cookies are not isolated by port, so another local web app could
otherwise drive the API with the owner's session. This middleware refuses:

- any request (HTTP or WebSocket) whose ``Host`` is not in the configured list,
  which also defeats DNS-rebinding pages;
- any cookie-bearing state-changing request, and any cookie-bearing WebSocket
  upgrade, whose ``Origin`` is absent or not an allowed origin.

Requests that carry no session cookie (the agent fleet and model runs, which
authenticate with signed tokens) are not browser sessions and skip the Origin
rule; they still need an allowed ``Host``. Logs name the refusal reason only.
"""

from __future__ import annotations

import json
import logging
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING

from roboco.api.auth.backend import SESSION_COOKIE_NAME

if TYPE_CHECKING:
    from collections.abc import Iterable

    from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_WS_POLICY_VIOLATION = 1008
_HTTP_BAD_REQUEST = 400
_HTTP_FORBIDDEN = 403


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope["headers"]:
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return None


def _carries_session_cookie(scope: Scope) -> bool:
    raw = _header(scope, b"cookie")
    if not raw:
        return False
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 - a malformed Cookie header is just "no session"
        return SESSION_COOKIE_NAME in raw
    return SESSION_COOKIE_NAME in jar


class LocalAccessGuardMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str],
    ) -> None:
        self._app = app
        self._hosts = frozenset(host.lower() for host in allowed_hosts)
        self._origins = frozenset(origin.lower() for origin in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        reason = self._refusal_reason(scope)
        if reason is None:
            await self._app(scope, receive, send)
            return
        logger.warning("Local access refused: %s (%s)", reason, scope["type"])
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})
            return
        status = _HTTP_BAD_REQUEST if reason == "host_not_allowed" else _HTTP_FORBIDDEN
        await _refuse(send, status, reason)

    def _refusal_reason(self, scope: Scope) -> str | None:
        host = (_header(scope, b"host") or "").lower()
        if host not in self._hosts:
            return "host_not_allowed"
        if not _carries_session_cookie(scope):
            return None
        if scope["type"] == "http" and scope["method"] in _SAFE_METHODS:
            return None
        origin = (_header(scope, b"origin") or "").lower()
        if origin not in self._origins:
            return "origin_not_allowed"
        return None


async def _refuse(send: Send, status: int, reason: str) -> None:
    body = json.dumps({"detail": reason}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
