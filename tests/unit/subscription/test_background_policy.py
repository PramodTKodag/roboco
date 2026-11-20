"""Features the subscription-only policy keeps off stay off in every path.

A forbidden feature sends model or project traffic somewhere other than the
subscription runtime. It must default to off, refuse to be switched on through
configuration, the settings store, or the shipped compose files, and be
reported as off when its state is read back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from roboco.config import Settings
from roboco.foundation.policy.subscription_policy import FORBIDDEN_FEATURE_FLAGS
from roboco.runtime.subscription.policy import (
    SubscriptionPolicyViolation,
    validate_subscription_policy,
)
from roboco.services.settings import SettingValidationError, validate_setting

REPO_ROOT = Path(__file__).parents[3]
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "docker-compose.registry.yml",
)
ENABLING_DEFAULT = re.compile(r":-\s*(true|1|yes|on)\s*\}", re.IGNORECASE)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.mark.parametrize("flag", FORBIDDEN_FEATURE_FLAGS)
def test_forbidden_flag_is_a_real_setting_that_defaults_off(flag: str) -> None:
    assert flag in Settings.model_fields, f"{flag} is not a setting"
    assert _settings().model_dump()[flag] is False


@pytest.mark.parametrize("flag", FORBIDDEN_FEATURE_FLAGS)
def test_forbidden_flag_cannot_be_switched_on_by_configuration(flag: str) -> None:
    # Constructed without validation: a flag with cross-field prerequisites
    # (the Mini App needs cloud auth) must still be refused on its own.
    armed = Settings.model_construct(**{flag: True})
    with pytest.raises(SubscriptionPolicyViolation) as caught:
        validate_subscription_policy(armed, environ={})
    assert caught.value.violations == (f"ROBOCO_{flag.upper()}",)


@pytest.mark.parametrize("flag", FORBIDDEN_FEATURE_FLAGS)
def test_forbidden_flag_cannot_be_stored_on(flag: str) -> None:
    with pytest.raises(SettingValidationError, match="subscription"):
        validate_setting(flag, "true")
    validate_setting(flag, "false")


def test_default_configuration_satisfies_the_policy() -> None:
    validate_subscription_policy(_settings(), environ={})


@pytest.mark.parametrize("compose_file", COMPOSE_FILES)
def test_shipped_compose_files_never_enable_a_forbidden_feature(
    compose_file: str,
) -> None:
    text = (REPO_ROOT / compose_file).read_text(encoding="utf-8")
    enabled = [
        flag
        for flag in FORBIDDEN_FEATURE_FLAGS
        for line in text.splitlines()
        if line.strip().startswith(f"ROBOCO_{flag.upper()}:")
        and ENABLING_DEFAULT.search(line)
    ]
    assert not enabled, f"{compose_file} defaults forbidden features on: {enabled}"
