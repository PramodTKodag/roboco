"""roboco-secretary MCP server — the Secretary's CEO-authority tools.

Tools: ``read_company_state`` / ``read_task`` / ``search_tasks`` (reads) and
``submit_directive`` (acts).
Each calls the backend ``/api/secretary/*`` routes; the backend gate-list
queues high-impact directive kinds for the CEO's confirmation and runs
low-risk ones directly. The backend-calling logic lives in ``secretary_driver``,
so this server only wraps those helpers as MCP tools.

Mounted for a secretary chat turn by the model-run supervisor through the
worker-tools bridge, which supplies the run-scoped credentials.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from roboco.agent_sdk.secretary_driver import (
    _do_read_state,
    _do_read_task,
    _do_search_tasks,
    _do_submit_directive,
)
from roboco.mcp.utils import configure_stdio_logging

# See flow_server.py: __name__ is already "__main__" at this point when run
# as the real stdio server, so this guards a plain in-process test import
# from clobbering the ambient structlog config for the whole process.
if __name__ == "__main__":
    configure_stdio_logging()

mcp = MCPServer("roboco-secretary")


@mcp.tool()
async def read_company_state() -> str:
    """Read a compact snapshot of company state.

    The charter (goals), task counts by status, pending pitches, and any
    directives awaiting the CEO's confirmation.
    """
    return json.dumps(await _do_read_state())


@mcp.tool()
async def read_task(task_id: str) -> str:
    """Read one task's full detail by its id — content, notes, plan,
    progress, and PR reference (Secretary FULL task access)."""
    return json.dumps(await _do_read_task(task_id))


@mcp.tool()
async def search_tasks(q: str, limit: int = 20) -> str:
    """Resolve a task NAME to concrete task ids.

    The CEO refers to tasks by name; search a title/description substring
    (min 2 chars) to get matching ids, then pass an id to read_task or to
    submit_directive's control_task. Returns up to 'limit' (default 20)
    matches under the 'tasks' key.
    """
    return json.dumps(await _do_search_tasks(q, limit))


@mcp.tool()
async def submit_directive(kind: str, payload: dict[str, Any] | None = None) -> str:
    """Act on the CEO's command.

    'kind' is one of: relay_message (payload: text), update_charter
    (payload: charter), control_task (payload: task_id, action[start|cancel|
    override|edit], status? for override, fields? for edit — edit accepts
    title/description/acceptance_criteria/priority/team/estimated_complexity/
    nature/assigned_to; assigned_to may be a UUID or an agent slug like
    "be-dev-1"), approve_pitch (payload: pitch_id, notes?), announce
    (payload: text). High-impact kinds (charter, control_task, approve_pitch,
    announce) are queued for the CEO's explicit confirmation; relay_message runs
    directly.
    """
    return json.dumps(await _do_submit_directive(kind, payload or {}))


if __name__ == "__main__":
    mcp.run()
