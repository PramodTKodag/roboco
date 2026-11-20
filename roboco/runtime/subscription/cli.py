"""The single builder of the official CLI invocation.

Every flag, path and setting the trusted service gives the CLI is produced here.
Nothing a task, a project or the model supplies reaches argv or the config files:
the prompt travels over stdin, the working directory is a fixed empty directory,
and built-in tools, project settings, project MCP servers, hooks, slash commands
and session persistence are switched off. The only tools a run can use are the
exact worker operations its manifest grants, served by the trusted bridge.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from roboco.runtime.subscription.contracts import MAX_RUN_TURNS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from roboco.runtime.subscription.contracts import RunSpec, WorkerOperation

CLAUDE_BINARY = Path("/opt/claude/claude")
# An empty, read-only directory: nothing in it for the CLI to discover.
RUN_CWD = Path("/var/lib/roboco/empty")
WORKER_MCP_SERVER_NAME = "roboco_worker"
_BRIDGE_MODULE = "roboco.runtime.subscription.worker_tools_mcp"
# The CLI gives the bridge a minimal environment, so the directory that holds the
# `roboco` package travels with it (this module sits at roboco/runtime/subscription).
_IMPORT_ROOT = Path(__file__).resolve().parents[3]

GATEWAY_SOCKET_ENV = "ROBOCO_GATEWAY_SOCKET"
RUN_CAPABILITY_ENV = "ROBOCO_RUN_CAPABILITY"


def mcp_tool_name(operation: WorkerOperation) -> str:
    return f"mcp__{WORKER_MCP_SERVER_NAME}__{operation.value}"


def expected_tool_names(operations: Iterable[WorkerOperation]) -> frozenset[str]:
    """Exact names the CLI may advertise; compared by equality, never by prefix."""
    return frozenset(mcp_tool_name(operation) for operation in operations)


def build_claude_argv(
    spec: RunSpec, *, mcp_config: Path | None, settings_file: Path
) -> list[str]:
    """Arguments after the binary. The prompt is not here: it goes over stdin."""
    argv = [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        spec.model,
        "--max-turns",
        str(MAX_RUN_TURNS),
        "--tools",
        "",
        "--strict-mcp-config",
    ]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config)]
    argv += [
        "--setting-sources",
        "",
        "--settings",
        str(settings_file),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--disable-slash-commands",
    ]
    return argv


def build_mcp_config(
    *, python: Path, socket_path: Path, capability: str
) -> dict[str, Any]:
    """MCP config naming only the trusted bridge. The capability stays in its env."""
    return {
        "mcpServers": {
            WORKER_MCP_SERVER_NAME: {
                "type": "stdio",
                "command": str(python),
                "args": ["-P", "-m", _BRIDGE_MODULE],
                "env": {
                    "PYTHONPATH": str(_IMPORT_ROOT),
                    GATEWAY_SOCKET_ENV: str(socket_path),
                    RUN_CAPABILITY_ENV: capability,
                },
            }
        }
    }


def build_settings(operations: Iterable[WorkerOperation]) -> dict[str, Any]:
    """Permissions allowing exactly the granted tools and nothing else."""
    return {
        "permissions": {"allow": sorted(expected_tool_names(operations))},
    }
