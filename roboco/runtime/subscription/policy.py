"""Subscription-only policy enforcement and the clean CLI environment."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from roboco.foundation.policy.subscription_policy import (
    FORBIDDEN_FEATURE_FLAGS,
    enabled_forbidden_flags,
    forbidden_env_names,
)
from roboco.runtime.subscription.contracts import CLI_MAX_RETRIES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from roboco.config import Settings

logger = logging.getLogger(__name__)

CLAUDE_HOME = "/home/claude"
CLAUDE_CONFIG_DIR = f"{CLAUDE_HOME}/.claude"
_CLI_PATH = "/usr/local/bin:/usr/bin:/bin"


class SubscriptionPolicyViolation(ValueError):
    """Unsafe configuration. Carries variable/setting names only, never values."""

    def __init__(self, violations: tuple[str, ...]) -> None:
        self.violations = violations
        super().__init__(
            "subscription-only policy violated by: " + ", ".join(violations)
        )


def validate_subscription_policy(
    settings: Settings, *, environ: Mapping[str, str] | None = None
) -> None:
    """Raise if any API key, provider override or fallback feature is configured.

    ``environ`` defaults to the process environment; tests pass their own.
    """
    env = os.environ if environ is None else environ
    violations: list[str] = []
    if settings.anthropic_api_key:
        violations.append("ROBOCO_ANTHROPIC_API_KEY")
    violations.extend(
        f"ROBOCO_{flag.upper()}"
        for flag in enabled_forbidden_flags(
            (name, bool(getattr(settings, name, False)))
            for name in FORBIDDEN_FEATURE_FLAGS
        )
    )
    violations.extend(forbidden_env_names(env))
    if violations:
        logger.error("Subscription policy blocked startup: %s", violations)
        raise SubscriptionPolicyViolation(tuple(violations))


def build_claude_env() -> dict[str, str]:
    """Environment for the official CLI, built from an allowlist.

    Nothing is inherited from the calling process: no credential, proxy, cloud
    or provider variable can reach the CLI by accident.
    """
    return {
        "PATH": _CLI_PATH,
        "HOME": CLAUDE_HOME,
        "CLAUDE_CONFIG_DIR": CLAUDE_CONFIG_DIR,
        "LANG": "C.UTF-8",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_MAX_RETRIES": str(CLI_MAX_RETRIES),
    }
