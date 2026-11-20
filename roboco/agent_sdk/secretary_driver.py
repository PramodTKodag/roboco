"""Secretary backend helpers — the CEO-authority calls behind the Secretary tools.

The Secretary's chat tools (``roboco.mcp.secretary_server``) call the backend
``/api/secretary/*`` routes on the CEO's behalf through these helpers:

* ``_do_read_state`` / ``_do_read_task`` / ``_do_search_tasks`` — reads;
  ``search_tasks`` resolves a task NAME to ids so a directive can target one
  (the CEO refers to tasks by name).
* ``_do_submit_directive`` — acts; the backend gate-list queues high-impact kinds
  for the CEO's confirmation and runs low-risk ones directly.

The logic lives in module-level helpers so it is unit-testable with
``httpx.MockTransport``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from roboco.agents_config import get_agent_team

_TIMEOUT = 30.0


def _api_base() -> str:
    return os.environ.get("ROBOCO_API_URL", "http://roboco-orchestrator:8000").rstrip(
        "/"
    )


def _headers() -> dict[str, str]:
    agent_id = os.environ.get("ROBOCO_AGENT_ID", "")
    headers = {
        "X-Agent-ID": agent_id,
        "X-Agent-Role": os.environ.get("ROBOCO_AGENT_ROLE", "secretary"),
    }
    team = get_agent_team(agent_id)
    if team:
        headers["X-Agent-Team"] = team
    token = os.environ.get("ROBOCO_AGENT_TOKEN")
    # See flow_server._build_headers: forwarding the "UNSIGNED" sentinel 401s
    # even in dev mode; omit so a missing token is accepted in dev.
    if token and token != "UNSIGNED":
        headers["X-Agent-Token"] = token
    return headers


async def _call_backend(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> Any:
    """Call ``/api/secretary{path}`` with the agent's auth; never raises.

    Returns the decoded JSON (an object for most routes, a list for the task
    search) or an ``{"error": ...}`` envelope on any HTTP/transport failure.
    """
    owns = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await http.request(
            method,
            f"{_api_base()}/api/secretary{path}",
            headers=_headers(),
            json=json_body,
            params=params,
            timeout=_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return {"error": "request_failed", "detail": str(exc)}
    finally:
        if owns:
            await http.aclose()
    if not resp.is_success:
        return {"error": f"http_{resp.status_code}", "detail": resp.text[:300]}
    return resp.json()


async def _do_read_state(*, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    result: dict[str, Any] = await _call_backend("GET", "/state", client=client)
    return result


async def _do_read_task(
    task_id: str, *, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = await _call_backend(
        "GET", f"/tasks/{task_id}", client=client
    )
    return result


async def _do_search_tasks(
    q: str,
    limit: int = 20,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Resolve a task NAME to concrete ids (title/description/id-prefix match).

    Wraps the list of matches under ``tasks`` so the tool result is an object;
    passes an ``{"error": ...}`` envelope straight through.
    """
    result = await _call_backend(
        "GET", "/tasks", params={"q": q, "limit": limit}, client=client
    )
    if isinstance(result, list):
        return {"tasks": result}
    return result if isinstance(result, dict) else {"error": "unexpected_response"}


async def _do_submit_directive(
    kind: str,
    payload: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = await _call_backend(
        "POST",
        "/directives",
        json_body={"kind": kind, "payload": payload},
        client=client,
    )
    return result
