"""The single CLI builder: fixed isolation flags, prompt off argv, exact tools."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from roboco.runtime.subscription.cli import (
    GATEWAY_SOCKET_ENV,
    RUN_CAPABILITY_ENV,
    WORKER_MCP_SERVER_NAME,
    build_claude_argv,
    build_mcp_config,
    build_settings,
    build_tool_manifest_file,
    expected_tool_names,
    mcp_tool_name,
    verb_tool_name,
)
from roboco.runtime.subscription.contracts import (
    CLI_MAX_RETRIES,
    MAX_RUN_TURNS,
    RunRequest,
    RunSpec,
    ToolManifest,
    VerbCredentials,
    VerbGrant,
    VerbServer,
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


def _manifest(
    operations: tuple[WorkerOperation, ...] = (),
    **grants: tuple[str, ...],
) -> ToolManifest:
    return ToolManifest(
        operations=operations,
        verb_grants=tuple(
            VerbGrant(server=VerbServer(server), verbs=verbs)
            for server, verbs in grants.items()
        ),
    )


def _credentials() -> VerbCredentials:
    return VerbCredentials(
        agent_id=uuid4(), role="backend_dev", token="synthetic-run-token-0123456789"
    )


def test_settings_allow_exactly_the_granted_tools() -> None:
    manifest = _manifest((WorkerOperation.READ_FILE, WorkerOperation.RUN_COMMAND))
    allow = build_settings(manifest)["permissions"]["allow"]
    assert allow == sorted(
        [
            f"mcp__{WORKER_MCP_SERVER_NAME}__read_file",
            f"mcp__{WORKER_MCP_SERVER_NAME}__run_command",
        ]
    )
    assert build_settings(None)["permissions"]["allow"] == []


def test_settings_carry_nothing_but_permissions() -> None:
    manifest = _manifest((WorkerOperation.READ_FILE,))
    assert set(build_settings(manifest)) == {"permissions"}


def test_expected_tool_names_are_compared_by_equality() -> None:
    names = expected_tool_names(_manifest((WorkerOperation.READ_FILE,)))
    assert names == frozenset({mcp_tool_name(WorkerOperation.READ_FILE)})
    assert "mcp__roboco_worker__read_file_extra" not in names
    assert "mcp__roboco_worker" not in names


def test_verb_tools_use_the_public_flow_name() -> None:
    manifest = _manifest(flow=("i_am_done", "pass_review"), do=("commit",))
    assert expected_tool_names(manifest) == frozenset(
        {
            "mcp__roboco_flow__i_am_done",
            "mcp__roboco_flow__pass",
            "mcp__roboco_do__commit",
        }
    )
    assert verb_tool_name(VerbServer.DO, "pass_review") == "mcp__roboco_do__pass_review"


def test_mcp_config_names_only_the_trusted_bridge() -> None:
    config = build_mcp_config(
        python=Path("/usr/local/bin/python"),
        socket_path=Path("/run/roboco/gateway/gateway.sock"),
        capability="synthetic-capability",
        manifest=_manifest((WorkerOperation.READ_FILE,)),
        credentials=None,
        orchestrator_url="",
        manifest_file=Path("/run/roboco/runs/x/tool-manifest.json"),
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


def test_mcp_config_launches_only_fixed_verb_server_modules() -> None:
    credentials = _credentials()
    config = build_mcp_config(
        python=Path("/usr/local/bin/python"),
        socket_path=Path("/run/roboco/gateway/gateway.sock"),
        capability=None,
        manifest=_manifest(flow=("i_am_done",), do=("commit",)),
        credentials=credentials,
        orchestrator_url="http://orchestrator:8000",
        manifest_file=Path("/run/roboco/runs/x/tool-manifest.json"),
    )
    servers = config["mcpServers"]
    assert set(servers) == {"roboco_flow", "roboco_do"}
    assert servers["roboco_flow"]["args"] == ["-P", "-m", "roboco.mcp.flow_server"]
    assert servers["roboco_do"]["args"] == ["-P", "-m", "roboco.mcp.do_server"]
    env = servers["roboco_flow"]["env"]
    assert env["ROBOCO_AGENT_TOKEN"] == credentials.token
    assert env["ROBOCO_ORCHESTRATOR_URL"] == "http://orchestrator:8000"
    assert env["ROBOCO_AGENT_ROLE"] == "backend_dev"


def test_verb_manifest_file_lists_each_servers_verbs() -> None:
    manifest = _manifest(flow=("i_am_done",), do=("commit", "read_task"))
    assert build_tool_manifest_file(manifest) == {
        "flow_tools": ["i_am_done"],
        "do_tools": ["commit", "read_task"],
    }
    assert build_tool_manifest_file(None) == {"flow_tools": [], "do_tools": []}


def test_credentials_never_appear_in_repr_or_logs() -> None:
    credentials = _credentials()
    assert credentials.token not in repr(credentials)


def test_verb_grants_need_credentials_and_an_allowed_purpose() -> None:
    manifest = _manifest(flow=("i_am_done",))
    with pytest.raises(ValidationError):
        RunRequest(spec=_spec(), manifest=manifest)
    with pytest.raises(ValidationError):
        RunRequest(
            spec=_spec(purpose="background", workspace_handle=None),
            manifest=manifest,
            verb_credentials=_credentials(),
        )
    RunRequest(spec=_spec(), manifest=manifest, verb_credentials=_credentials())


@pytest.mark.parametrize("verb", ["Bad-Name", "", "a" * 80, "x/../y"])
def test_verb_names_are_validated(verb: str) -> None:
    with pytest.raises(ValidationError):
        VerbGrant(server=VerbServer.FLOW, verbs=(verb,))


def test_a_server_cannot_be_granted_twice() -> None:
    with pytest.raises(ValidationError):
        ToolManifest(
            verb_grants=(
                VerbGrant(server=VerbServer.FLOW, verbs=("a",)),
                VerbGrant(server=VerbServer.FLOW, verbs=("b",)),
            )
        )


def test_cli_environment_is_an_allowlist_with_a_bounded_retry_count() -> None:
    env = build_claude_env()
    assert env["CLAUDE_CODE_MAX_RETRIES"] == str(CLI_MAX_RETRIES) == "2"
    assert not [name for name in env if name.startswith(("ANTHROPIC_", "AWS_"))]


def _chat_manifest(server: VerbServer, verbs: tuple[str, ...]) -> ToolManifest:
    return ToolManifest(verb_grants=(VerbGrant(server=server, verbs=verbs),))


def test_chat_servers_launch_only_their_fixed_modules() -> None:
    credentials = _credentials()
    for server, module in (
        (VerbServer.INTAKE, "roboco.mcp.intake_server"),
        (VerbServer.SECRETARY, "roboco.mcp.secretary_server"),
    ):
        config = build_mcp_config(
            python=Path("/usr/local/bin/python"),
            socket_path=Path("/run/roboco/gateway/gateway.sock"),
            capability=None,
            manifest=_chat_manifest(server, ("propose_draft",)),
            credentials=credentials,
            orchestrator_url="http://orchestrator:8000",
            manifest_file=Path("/run/roboco/runs/x/tool-manifest.json"),
            session_id="3f2a9c10aabbccdd",
        )
        launched = config["mcpServers"][f"roboco_{server.value}"]
        assert launched["args"] == ["-P", "-m", module]
        assert launched["env"]["ROBOCO_API_URL"] == "http://orchestrator:8000"


def test_only_the_intake_server_receives_the_chat_session_id() -> None:
    credentials = _credentials()

    def env_for(server: VerbServer) -> dict[str, str]:
        config = build_mcp_config(
            python=Path("/usr/local/bin/python"),
            socket_path=Path("/run/roboco/gateway/gateway.sock"),
            capability=None,
            manifest=_chat_manifest(server, ("propose_draft",)),
            credentials=credentials,
            orchestrator_url="http://orchestrator:8000",
            manifest_file=Path("/run/roboco/runs/x/tool-manifest.json"),
            session_id="3f2a9c10aabbccdd",
        )
        return config["mcpServers"][f"roboco_{server.value}"]["env"]

    assert env_for(VerbServer.INTAKE)["ROBOCO_PROMPTER_SESSION_ID"] == (
        "3f2a9c10aabbccdd"
    )
    assert "ROBOCO_PROMPTER_SESSION_ID" not in env_for(VerbServer.SECRETARY)


def test_chat_verbs_use_their_own_server_names() -> None:
    assert verb_tool_name(VerbServer.INTAKE, "propose_draft") == (
        "mcp__roboco_intake__propose_draft"
    )
    assert verb_tool_name(VerbServer.SECRETARY, "submit_directive") == (
        "mcp__roboco_secretary__submit_directive"
    )


def test_an_intake_grant_needs_a_session_id_and_nothing_else_may_have_one() -> None:
    manifest = _chat_manifest(VerbServer.INTAKE, ("propose_draft",))
    spec = _spec(purpose="intake", workspace_handle=None, project_id=None)
    with pytest.raises(ValidationError):
        RunRequest(spec=spec, manifest=manifest, verb_credentials=_credentials())
    RunRequest(
        spec=spec,
        manifest=manifest,
        verb_credentials=_credentials(),
        session_id="3f2a9c10aabbccdd",
    )
    with pytest.raises(ValidationError):
        RunRequest(spec=_spec(), session_id="3f2a9c10aabbccdd")


@pytest.mark.parametrize(
    "session_id", ["short", "a/b/c/d/e/f/g", "x" * 65, "sp ace12345"]
)
def test_chat_session_ids_are_validated(session_id: str) -> None:
    with pytest.raises(ValidationError):
        RunRequest(
            spec=_spec(purpose="intake", workspace_handle=None, project_id=None),
            manifest=_chat_manifest(VerbServer.INTAKE, ("propose_draft",)),
            verb_credentials=_credentials(),
            session_id=session_id,
        )


def test_a_chat_run_cannot_be_granted_another_purposes_server() -> None:
    with pytest.raises(ValidationError):
        RunRequest(
            spec=_spec(purpose="secretary", workspace_handle=None, project_id=None),
            manifest=_chat_manifest(VerbServer.INTAKE, ("propose_draft",)),
            verb_credentials=_credentials(),
            session_id="3f2a9c10aabbccdd",
        )
