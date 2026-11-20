"""Redacted subscription status from the official CLI's own auth-status command.

Credential files are never read here. The CLI runs with the allowlisted
environment, its stderr is discarded and its stdout is reduced to a verdict:
no raw output, email or token ever reaches a log or a returned status.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from datetime import UTC, datetime
from pathlib import Path

from roboco.config import settings
from roboco.foundation.policy.subscription_policy import (
    SUBSCRIPTION_AUTH_METHOD,
    classify_auth_status_text,
)
from roboco.runtime.subscription.contracts import SubscriptionState, SubscriptionStatus
from roboco.runtime.subscription.policy import EgressRoute, build_claude_env

logger = logging.getLogger(__name__)

_MAX_STATUS_OUTPUT_BYTES = 64 * 1024

_VERDICT_TO_STATE: dict[str, SubscriptionState] = {
    "subscription": SubscriptionState.READY,
    "login_required": SubscriptionState.LOGIN_REQUIRED,
    "rejected_auth_method": SubscriptionState.POLICY_BLOCKED,
    "rejected_mixed_auth": SubscriptionState.POLICY_BLOCKED,
    "rejected_provider": SubscriptionState.POLICY_BLOCKED,
    "unknown_schema": SubscriptionState.POLICY_BLOCKED,
}


def _status(
    state: SubscriptionState, reason_code: str | None, *, auth_method: str | None = None
) -> SubscriptionStatus:
    status = SubscriptionStatus(
        state=state,
        authenticated=state is SubscriptionState.READY,
        auth_method=auth_method,
        checked_at=datetime.now(UTC),
        reason_code=reason_code,
    )
    logger.info("Subscription status: state=%s reason=%s", state.value, reason_code)
    return status


def _kill_process_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


async def _run_status_command(
    cli_path: Path, timeout_seconds: float, egress: EgressRoute | None
) -> tuple[int, bytes] | str:
    """Return (exit code, stdout) or a reason code when the command can't run."""
    try:
        process = await asyncio.create_subprocess_exec(
            str(cli_path),
            "auth",
            "status",
            "--json",
            env=build_claude_env(egress),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return "cli_unavailable"
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        _kill_process_group(process.pid)
        await process.wait()
        return "status_timeout"
    return process.returncode or 0, stdout


async def inspect_subscription(
    *,
    cli_path: Path | None = None,
    timeout_seconds: float | None = None,
    egress: EgressRoute | None = None,
) -> SubscriptionStatus:
    """Ask the official CLI who is logged in; only a clean subscription is ready."""
    command = cli_path or Path(settings.subscription_cli_path)
    timeout = timeout_seconds or settings.subscription_status_timeout_seconds
    outcome = await _run_status_command(command, timeout, egress)
    if isinstance(outcome, str):
        return _status(SubscriptionState.CONFIGURATION_ERROR, outcome)
    exit_code, stdout = outcome
    if len(stdout) > _MAX_STATUS_OUTPUT_BYTES:
        return _status(SubscriptionState.POLICY_BLOCKED, "unknown_schema")
    verdict = classify_auth_status_text(stdout.decode("utf-8", errors="replace"))
    state = _VERDICT_TO_STATE[verdict]
    if state is SubscriptionState.READY:
        if exit_code != 0:
            return _status(SubscriptionState.CONFIGURATION_ERROR, "status_exit_nonzero")
        return _status(state, None, auth_method=SUBSCRIPTION_AUTH_METHOD)
    return _status(state, verdict)
