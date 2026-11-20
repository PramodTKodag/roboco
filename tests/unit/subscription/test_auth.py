"""Auth-status classification and the redacted status inspector."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from typing import TYPE_CHECKING

import pytest
from roboco.foundation.policy.subscription_policy import (
    classify_auth_status,
    classify_auth_status_text,
)
from roboco.runtime.subscription.auth import inspect_subscription
from roboco.runtime.subscription.contracts import (
    SubscriptionState,
    SubscriptionStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

SUBSCRIPTION = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
}
SECRET = "sk-ant-SYNTHETIC-must-never-surface"


def _fake_cli(
    tmp_path: Path, stdout: str, *, exit_code: int = 0, sleep: float = 0.0
) -> Path:
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f"sleep {sleep}\n"
        f"cat <<'PAYLOAD'\n{stdout}\nPAYLOAD\n"
        f"echo 'email: owner@example.test token: {SECRET}' >&2\n"
        f"exit {exit_code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _inspect(cli: Path, *, timeout: float = 5.0) -> SubscriptionStatus:
    return asyncio.run(inspect_subscription(cli_path=cli, timeout_seconds=timeout))


def test_clean_subscription_is_ready(tmp_path: Path) -> None:
    status = _inspect(_fake_cli(tmp_path, json.dumps(SUBSCRIPTION)))
    assert status.state is SubscriptionState.READY
    assert status.authenticated is True
    assert status.auth_method == "claude.ai"
    assert status.reason_code is None


def test_mixed_auth_is_rejected(tmp_path: Path) -> None:
    payload = {**SUBSCRIPTION, "apiKeySource": "apiKeyHelper"}
    status = _inspect(_fake_cli(tmp_path, json.dumps(payload)))
    assert status.state is SubscriptionState.POLICY_BLOCKED
    assert status.reason_code == "rejected_mixed_auth"
    assert status.authenticated is False


def test_console_auth_is_rejected(tmp_path: Path) -> None:
    payload = {**SUBSCRIPTION, "authMethod": "console"}
    status = _inspect(_fake_cli(tmp_path, json.dumps(payload)))
    assert status.state is SubscriptionState.POLICY_BLOCKED
    assert status.authenticated is False


def test_api_key_auth_is_rejected(tmp_path: Path) -> None:
    payload = {
        "loggedIn": True,
        "authMethod": "api_key",
        "apiProvider": "firstParty",
        "apiKeySource": "ANTHROPIC_API_KEY",
    }
    status = _inspect(_fake_cli(tmp_path, json.dumps(payload)))
    assert status.state is SubscriptionState.POLICY_BLOCKED
    assert status.reason_code == "rejected_auth_method"


def test_logged_out_requires_login(tmp_path: Path) -> None:
    payload = {"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}
    status = _inspect(_fake_cli(tmp_path, json.dumps(payload), exit_code=1))
    assert status.state is SubscriptionState.LOGIN_REQUIRED
    assert status.authenticated is False


@pytest.mark.parametrize("stdout", ["not json", "[1, 2]", "{}", '{"loggedIn": "yes"}'])
def test_unknown_auth_status_blocks(tmp_path: Path, stdout: str) -> None:
    status = _inspect(_fake_cli(tmp_path, stdout))
    assert status.state is SubscriptionState.POLICY_BLOCKED
    assert status.reason_code == "unknown_schema"
    assert status.authenticated is False


def test_ready_requires_a_zero_exit_code(tmp_path: Path) -> None:
    status = _inspect(_fake_cli(tmp_path, json.dumps(SUBSCRIPTION), exit_code=3))
    assert status.state is not SubscriptionState.READY


def test_missing_cli_is_a_configuration_error(tmp_path: Path) -> None:
    status = _inspect(tmp_path / "does-not-exist")
    assert status.state is SubscriptionState.CONFIGURATION_ERROR
    assert status.reason_code == "cli_unavailable"


def test_hung_cli_times_out_as_configuration_error(tmp_path: Path) -> None:
    status = _inspect(
        _fake_cli(tmp_path, json.dumps(SUBSCRIPTION), sleep=5), timeout=0.3
    )
    assert status.state is SubscriptionState.CONFIGURATION_ERROR
    assert status.reason_code == "status_timeout"


def test_status_is_redacted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    payload = {**SUBSCRIPTION, "email": "owner@example.test", "token": SECRET}
    with caplog.at_level(logging.DEBUG):
        status = _inspect(_fake_cli(tmp_path, json.dumps(payload)))
    dumped = status.model_dump_json()
    assert set(json.loads(dumped)) == {
        "state",
        "authenticated",
        "auth_method",
        "checked_at",
        "reason_code",
    }
    for leaked in (SECRET, "owner@example.test"):
        assert leaked not in dumped
        assert leaked not in caplog.text


def test_classifier_table() -> None:
    assert classify_auth_status(SUBSCRIPTION) == "subscription"
    assert classify_auth_status({**SUBSCRIPTION, "apiProvider": "bedrock"}) == (
        "rejected_provider"
    )
    assert classify_auth_status_text("garbage") == "unknown_schema"
