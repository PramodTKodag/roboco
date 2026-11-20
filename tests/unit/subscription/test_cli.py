"""The single CLI builder: fixed isolation flags, prompt off argv, exact tools."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from roboco.runtime.subscription.cli import (
    GATEWAY_SOCKET_ENV,
    RUN_CAPABILITY_ENV,
    WORKER_MCP_SERVER_NAME,
    build_claude_argv,
    build_mcp_config,
    build_settings,
    expected_tool_names,
    mcp_tool_name,
)
from roboco.runtime.subscription.contracts import (
    CLI_MAX_RETRIES,
    MAX_RUN_TURNS,
    RunSpec,
    WorkerOperation,
)
from roboco.runtime.subscription.policy import build_claude_env

SETTINGS = Path("/run/roboco/runs/x/settings.json")
MCP = Path("/run/roboco/runs/x/mcp.json")
SECRET_PROMPT = "synthetic-prompt-sentinel-91ab"


def _spec(**overrides: object) -> RunSpec:
    values: dict[str, object] = {
        "run_id": uuid4(),
        "project_id": uuid4(),
        "task_id": uuid4(),
        "agent_id": uuid4(),
        "purpose": "delivery",
        "model": "claude-sonnet-5-5",
        "prompt": SECRET_PROMPT,
        "workspace_handle": uuid4(),
    }
    values.update(overrides)
    return RunSpec(**values)  # type: ignore[arg-type]


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_isolation_flags_are_always_present() -> None:
    argv = build_claude_argv(_spec(), mcp_config=MCP, settings_file=SETTINGS)
    assert _flag_value(argv, "--tools") == ""
    assert "--strict-mcp-config" in argv
    assert _flag_value(argv, "--setting-sources") == ""
    assert _flag_value(argv, "--permission-mode") == "dontAsk"
    assert _flag_value(argv, "--permission-prompts") == "none"
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv
    assert _flag_value(argv, "--max-turns") == str(MAX_RUN_TURNS)
    assert _flag_value(argv, "--settings") == str(SETTINGS)
    assert _flag_value(argv, "--mcp-config") == str(MCP)


def test_dangerous_flags_never_appear() -> None:
    argv = build_claude_argv(_spec(), mcp_config=MCP, settings_file=SETTINGS)
    joined = " ".join(argv)
    for forbidden in (
        "--bare",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--add-dir",
        "--resume",
        "--continue",
        "--fallback-model",
        "--plugin-dir",
        "--agents",
        "auto",
    ):
        assert forbidden not in joined


def test_prompt_and_identities_never_reach_argv() -> None:
    spec = _spec()
    argv = build_claude_argv(spec, mcp_config=MCP, settings_file=SETTINGS)
    joined = " ".join(argv)
    assert SECRET_PROMPT not in joined
    for identity in (spec.run_id, spec.project_id, spec.task_id, spec.workspace_handle):
        assert str(identity) not in joined


def test_model_is_the_specs_exact_id() -> None:
    argv = build_claude_argv(
        _spec(model="claude-opus-5-5"), mcp_config=MCP, settings_file=SETTINGS
    )
    assert _flag_value(argv, "--model") == "claude-opus-5-5"


def test_a_tool_free_run_has_no_mcp_config_but_stays_strict() -> None:
    argv = build_claude_argv(
        _spec(purpose="background", workspace_handle=None),
        mcp_config=None,
        settings_file=SETTINGS,
    )
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" in argv
    assert _flag_value(argv, "--tools") == ""


def test_settings_allow_exactly_the_granted_tools() -> None:
    granted = [WorkerOperation.READ_FILE, WorkerOperation.RUN_COMMAND]
    allow = build_settings(granted)["permissions"]["allow"]
    assert allow == sorted(
        [
            f"mcp__{WORKER_MCP_SERVER_NAME}__read_file",
            f"mcp__{WORKER_MCP_SERVER_NAME}__run_command",
        ]
    )
    assert build_settings([])["permissions"]["allow"] == []


def test_settings_carry_nothing_but_permissions() -> None:
    assert set(build_settings([WorkerOperation.READ_FILE])) == {"permissions"}


def test_expected_tool_names_are_compared_by_equality() -> None:
    names = expected_tool_names([WorkerOperation.READ_FILE])
    assert names == frozenset({mcp_tool_name(WorkerOperation.READ_FILE)})
    assert "mcp__roboco_worker__read_file_extra" not in names
    assert "mcp__roboco_worker" not in names


def test_mcp_config_names_only_the_trusted_bridge() -> None:
    config = build_mcp_config(
        python=Path("/usr/local/bin/python"),
        socket_path=Path("/run/roboco/gateway/gateway.sock"),
        capability="synthetic-capability",
    )
    assert list(config["mcpServers"]) == [WORKER_MCP_SERVER_NAME]
    server = config["mcpServers"][WORKER_MCP_SERVER_NAME]
    assert server["type"] == "stdio"
    assert server["args"][:2] == ["-P", "-m"]
    assert set(server["env"]) == {
        "PYTHONPATH",
        GATEWAY_SOCKET_ENV,
        RUN_CAPABILITY_ENV,
    }


def test_cli_environment_is_an_allowlist_with_a_bounded_retry_count() -> None:
    env = build_claude_env()
    assert env["CLAUDE_CODE_MAX_RETRIES"] == str(CLI_MAX_RETRIES) == "2"
    assert not [name for name in env if name.startswith(("ANTHROPIC_", "AWS_"))]
