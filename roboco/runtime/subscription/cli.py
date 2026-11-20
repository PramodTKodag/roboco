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

from roboco.foundation.policy.flow_names import public_flow_verb
from roboco.runtime.subscription.contracts import MAX_RUN_TURNS, VerbServer

if TYPE_CHECKING:
    from roboco.runtime.subscription.contracts import (
        RunSpec,
        ToolManifest,
        VerbCredentials,
        WorkerOperation,
    )

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
TOOL_MANIFEST_FILE = "tool-manifest.json"

# The code each verb server launches is fixed here, never chosen by a controller.
_VERB_SERVER_MODULES: dict[VerbServer, str] = {
    VerbServer.FLOW: "roboco.mcp.flow_server",
    VerbServer.DO: "roboco.mcp.do_server",
}


def mcp_tool_name(operation: WorkerOperation) -> str:
    return f"mcp__{WORKER_MCP_SERVER_NAME}__{operation.value}"


def verb_server_name(server: VerbServer) -> str:
    return f"roboco_{server.value}"


def verb_tool_name(server: VerbServer, verb: str) -> str:
    """The name the CLI advertises for a verb; flow intents use their public name."""
    public = public_flow_verb(verb) if server is VerbServer.FLOW else verb
    return f"mcp__{verb_server_name(server)}__{public}"


def expected_tool_names(manifest: ToolManifest | None) -> frozenset[str]:
    """Exact names the CLI may advertise; compared by equality, never by prefix."""
    if manifest is None:
        return frozenset()
    worker = {mcp_tool_name(operation) for operation in manifest.operations}
    verbs = {
        verb_tool_name(grant.server, verb)
        for grant in manifest.verb_grants
        for verb in grant.verbs
    }
    return frozenset(worker | verbs)


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


def _bridge_server(
    *, python: Path, socket_path: Path, capability: str
) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": str(python),
        "args": ["-P", "-m", _BRIDGE_MODULE],
        "env": {
            "PYTHONPATH": str(_IMPORT_ROOT),
            GATEWAY_SOCKET_ENV: str(socket_path),
            RUN_CAPABILITY_ENV: capability,
        },
    }


def _verb_server(
    *,
    python: Path,
    server: VerbServer,
    credentials: VerbCredentials,
    orchestrator_url: str,
    manifest_file: Path,
) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": str(python),
        "args": ["-P", "-m", _VERB_SERVER_MODULES[server]],
        "env": {
            "PYTHONPATH": str(_IMPORT_ROOT),
            "ROBOCO_AGENT_ID": str(credentials.agent_id),
            "ROBOCO_AGENT_ROLE": credentials.role,
            "ROBOCO_AGENT_TOKEN": credentials.token,
            "ROBOCO_ORCHESTRATOR_URL": orchestrator_url,
            "ROBOCO_TOOL_MANIFEST_PATH": str(manifest_file),
        },
    }


def build_mcp_config(  # noqa: PLR0913 - one builder for every trusted server
    *,
    python: Path,
    socket_path: Path,
    capability: str | None,
    manifest: ToolManifest | None,
    credentials: VerbCredentials | None,
    orchestrator_url: str,
    manifest_file: Path,
) -> dict[str, Any]:
    """MCP config naming only trusted servers. Secrets stay in their own env."""
    servers: dict[str, Any] = {}
    if capability is not None:
        servers[WORKER_MCP_SERVER_NAME] = _bridge_server(
            python=python, socket_path=socket_path, capability=capability
        )
    if manifest is not None and credentials is not None:
        for grant in manifest.verb_grants:
            servers[verb_server_name(grant.server)] = _verb_server(
                python=python,
                server=grant.server,
                credentials=credentials,
                orchestrator_url=orchestrator_url,
                manifest_file=manifest_file,
            )
    return {"mcpServers": servers}


def build_tool_manifest_file(manifest: ToolManifest | None) -> dict[str, list[str]]:
    """The verb manifest the flow and do servers read at start."""
    verbs = {
        grant.server: list(grant.verbs)
        for grant in (manifest.verb_grants if manifest else ())
    }
    return {
        "flow_tools": verbs.get(VerbServer.FLOW, []),
        "do_tools": verbs.get(VerbServer.DO, []),
    }


def build_settings(manifest: ToolManifest | None) -> dict[str, Any]:
    """Permissions allowing exactly the granted tools and nothing else."""
    return {
        "permissions": {"allow": sorted(expected_tool_names(manifest))},
    }
