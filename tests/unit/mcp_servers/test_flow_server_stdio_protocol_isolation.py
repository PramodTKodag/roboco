"""Real subprocess smoke: flow_server's stdout must carry only JSON-RPC.

The MCP stdio transport requires stdout to hold protocol messages exclusively.
structlog falls back to its own un-configured default (a PrintLogger on
stdout) unless a stdio server explicitly redirects it, and the module-import
warning that fires for a manifest naming an unimplemented verb would otherwise be the
first bytes on the wire, before any JSON-RPC frame. In-process tests can't
catch this: they never go through a real OS pipe, so a stray print() on the
wrong stream doesn't show up. Only a real subprocess does.
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_STARTUP_TIMEOUT_SECONDS = 8.0


def test_flow_server_stdout_carries_only_the_jsonrpc_response(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "tool-manifest.json"
    # The unknown verb makes the server log an import-time warning, the exact
    # output that must not reach stdout.
    manifest.write_text(
        json.dumps({"flow_tools": ["i_am_idle", "not_a_real_verb"], "do_tools": []})
    )
    env = os.environ.copy()
    env["ROBOCO_TOOL_MANIFEST_PATH"] = str(manifest)
    env["ROBOCO_AGENT_ID"] = "test-agent"
    env["ROBOCO_AGENT_ROLE"] = "developer"

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.0.1"},
        },
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "roboco.mcp.flow_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        ready = sel.select(timeout=_STARTUP_TIMEOUT_SECONDS)
        assert ready, (
            f"flow_server produced no stdout within "
            f"{_STARTUP_TIMEOUT_SECONDS}s of the initialize request"
        )
        first_line = proc.stdout.readline()
    finally:
        proc.terminate()
        _, stderr = proc.communicate(timeout=5)

    parsed = json.loads(first_line)
    assert "result" in parsed, (
        f"stdout's first line was not a JSON-RPC result, protocol stream "
        f"corrupted: {first_line!r}"
    )
    assert "flow_server: registered tools" in stderr, (
        f"registered-tools log line missing from stderr (it must not land "
        f"on stdout instead): {stderr!r}"
    )
