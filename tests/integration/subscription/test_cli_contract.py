"""Contract tests for the pinned official Claude CLI (Task 1).

Two layers, reported separately and never conflated:

* Pure tests always run: artifact integrity, auth-status classification and
  stream parsing.
* Container tests run the REAL pinned CLI binary inside locked-down Docker
  containers on an internal-only network against a SYNTHETIC provider. They
  are skipped with an explicit reason when Docker or the staged binary is
  missing. A skip is not a pass.

No test here authenticates, spends subscription allowance, or reaches the
public internet. Live subscription validation is a separate opt-in
(``ROBOCO_LIVE_SUBSCRIPTION_TESTS=1``) covered in Task 13.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).parents[3]
_PROBE_PATH = _REPO_ROOT / "scripts" / "personal" / "probe_cli_contract.py"

_spec = importlib.util.spec_from_file_location("probe_cli_contract", _PROBE_PATH)
assert _spec is not None and _spec.loader is not None
probe = importlib.util.module_from_spec(_spec)
sys.modules["probe_cli_contract"] = probe
_spec.loader.exec_module(probe)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Pure layer
# ---------------------------------------------------------------------------


def test_pinned_versions_are_exact() -> None:
    assert probe.PINNED_CLI_VERSION == "2.1.311"
    assert "latest" not in probe.PROBE_BASE_IMAGE
    assert "@sha256:" in probe.PROBE_BASE_IMAGE
    assert "@sha256:" in probe.FAKE_PROVIDER_IMAGE


def test_integrity_check_accepts_matching_and_rejects_tampered_bytes() -> None:
    data = b"synthetic artifact bytes"
    sri = (
        "sha512-"
        + __import__("base64").b64encode(hashlib.sha512(data).digest()).decode()
    )
    assert probe.matches_subresource_integrity(data, sri) is True
    assert probe.matches_subresource_integrity(data + b"x", sri) is False
    assert probe.matches_subresource_integrity(data, "sha512-AAAA") is False
    assert probe.matches_subresource_integrity(data, "md5-whatever") is False


@pytest.mark.parametrize(
    ("payload", "verdict"),
    [
        (
            {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"},
            "login_required",
        ),
        (
            {
                "loggedIn": True,
                "authMethod": "api_key",
                "apiProvider": "firstParty",
                "apiKeySource": "ANTHROPIC_API_KEY",
            },
            "rejected_auth_method",
        ),
        (
            {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"},
            "subscription",
        ),
        # Subscription method but a helper/inherited key source is also present.
        (
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "apiKeySource": "apiKeyHelper",
            },
            "rejected_mixed_auth",
        ),
        # Unobserved methods are unknown (still blocked); other providers rejected.
        (
            {"loggedIn": True, "authMethod": "console", "apiProvider": "firstParty"},
            "unknown_schema",
        ),
        (
            {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
            "rejected_provider",
        ),
        (
            {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "vertex"},
            "rejected_provider",
        ),
        # Schema drift must block, never be guessed at.
        (
            {
                "loggedIn": True,
                "authMethod": "future_method",
                "apiProvider": "firstParty",
            },
            "unknown_schema",
        ),
        ({"loggedIn": True, "apiProvider": "firstParty"}, "unknown_schema"),
        ({"authMethod": "claude.ai", "apiProvider": "firstParty"}, "unknown_schema"),
        (
            {"loggedIn": "yes", "authMethod": "claude.ai", "apiProvider": "firstParty"},
            "unknown_schema",
        ),
        ({}, "unknown_schema"),
        # Logged-out but claiming subscription method is contradictory.
        (
            {"loggedIn": False, "authMethod": "claude.ai", "apiProvider": "firstParty"},
            "unknown_schema",
        ),
    ],
)
def test_auth_status_classification_blocks_everything_but_clean_subscription(
    payload: dict, verdict: str
) -> None:
    assert probe.classify_auth_status(payload) == verdict


def test_unparseable_auth_status_is_unknown_schema() -> None:
    assert probe.classify_auth_status_text("not json") == "unknown_schema"
    assert probe.classify_auth_status_text("[1,2]") == "unknown_schema"


def test_stream_parser_extracts_tool_surface_and_failed_tool_results() -> None:
    init = {
        "type": "system",
        "subtype": "init",
        "tools": ["mcp__probe__echo"],
        "mcp_servers": [],
    }
    denied = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "is_error": True,
                    "content": "<tool_use_error>No such tool: Bash</tool_use_error>",
                }
            ]
        },
    }
    stdout = "\n".join(json.dumps(e) for e in (init, denied, {"type": "result"})) + "\n"
    observed = probe.parse_stream(stdout)
    assert observed.advertised_tools == ["mcp__probe__echo"]
    assert observed.tool_results[0].is_error is True
    assert "No such tool" in observed.tool_results[0].text
    assert observed.terminal_event_seen is True


def test_stream_parser_marks_missing_terminal_event() -> None:
    init = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
    observed = probe.parse_stream(json.dumps(init) + "\n")
    assert observed.terminal_event_seen is False


def test_argv_builder_never_emits_bypass_or_bare_modes() -> None:
    argv = probe.build_probe_argv(model="claude-sonnet-4-5")
    joined = " ".join(argv)
    for forbidden in ("--bare", "bypassPermissions", "--dangerously", "auto"):
        assert forbidden not in joined
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--no-session-persistence" in argv
    assert "--disable-slash-commands" in argv


# ---------------------------------------------------------------------------
# Container layer: real pinned binary, synthetic provider, internal network
# ---------------------------------------------------------------------------

_container_skip_reason = probe.container_layer_unavailable_reason()
container_layer = pytest.mark.skipif(
    _container_skip_reason is not None,
    reason=_container_skip_reason or "container layer available",
)


@pytest.fixture(scope="module")
def sandbox() -> Iterator[Any]:
    with probe.ProbeSandbox() as box:
        yield box


@container_layer
def test_staged_binary_matches_pinned_digest() -> None:
    assert probe.staged_binary_sha256() == probe.PINNED_BINARY_SHA256[probe.host_arch()]


@container_layer
def test_logged_out_status_requires_login(sandbox: Any) -> None:
    result = sandbox.run_cli(["auth", "status", "--json"], env={})
    assert result.returncode != 0
    assert probe.classify_auth_status_text(result.stdout) == "login_required"


@container_layer
def test_inherited_api_key_is_refused(sandbox: Any) -> None:
    result = sandbox.run_cli(
        ["auth", "status", "--json"], env={"ANTHROPIC_API_KEY": "sk-ant-SYNTHETIC-0000"}
    )
    assert probe.classify_auth_status_text(result.stdout) == "rejected_auth_method"


@container_layer
def test_only_the_explicit_mcp_tool_is_advertised(sandbox: Any) -> None:
    run = sandbox.run_prompt(fake_tool_call=None)
    assert run.observed.advertised_tools == ["mcp__probe__echo"]
    assert run.provider_requests, "synthetic provider saw no request"
    assert all(r["tools"] == ["mcp__probe__echo"] for r in run.provider_requests)


@container_layer
@pytest.mark.parametrize(
    "tool_name",
    [
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "Task",
        "Agent",
    ],
)
def test_builtin_tool_attempts_never_execute(sandbox: Any, tool_name: str) -> None:
    run = sandbox.run_prompt(fake_tool_call=f"{tool_name}|{{}}")
    assert run.observed.tool_results, "model tool call produced no tool_result"
    assert all(r.is_error for r in run.observed.tool_results)
    assert not run.mcp_tool_calls
    assert run.observed.terminal_event_seen


@container_layer
def test_allowed_mcp_tool_executes_through_dontask(sandbox: Any) -> None:
    run = sandbox.run_prompt(fake_tool_call='mcp__probe__echo|{"message":"hi"}')
    assert [c["name"] for c in run.mcp_tool_calls] == ["echo"]
    assert not any(r.is_error for r in run.observed.tool_results)


@container_layer
def test_hostile_project_config_is_inert(sandbox: Any) -> None:
    run = sandbox.run_prompt(
        fake_tool_call=None, project_dir=sandbox.hostile_project_dir()
    )
    assert run.observed.advertised_tools == ["mcp__probe__echo"]
    assert run.unexpected_paths == [], run.unexpected_paths
    assert not run.mcp_tool_calls


@container_layer
def test_hostile_project_config_is_live_without_the_flags(
    sandbox: Any,
) -> None:
    """Control: hostile fixture is effective, so the inert result means something."""
    run = sandbox.run_prompt_without_isolation_flags(
        project_dir=sandbox.hostile_project_dir()
    )
    assert run.unexpected_paths, (
        "hostile fixture had no effect; the inertness test proves nothing"
    )
