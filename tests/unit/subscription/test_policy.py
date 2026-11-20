"""Subscription-only policy: unsafe configuration must block, by name only."""

from __future__ import annotations

import pytest
from roboco.config import Settings
from roboco.runtime.subscription.policy import (
    SubscriptionPolicyViolation,
    build_claude_env,
    validate_subscription_policy,
)
from roboco.services.settings import SettingValidationError, validate_setting

SECRET_VALUE = "sk-ant-SYNTHETIC-must-never-be-echoed"


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_clean_configuration_is_accepted() -> None:
    validate_subscription_policy(_settings(), environ={"PATH": "/usr/bin"})


def test_api_key_setting_is_rejected_without_echoing_its_value() -> None:
    with pytest.raises(SubscriptionPolicyViolation) as caught:
        validate_subscription_policy(
            _settings(anthropic_api_key=SECRET_VALUE), environ={}
        )
    assert caught.value.violations == ("ROBOCO_ANTHROPIC_API_KEY",)
    assert SECRET_VALUE not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_GATEWAY_TOKEN",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        "CLAUDE_CODE_FUTURE_UNKNOWN_SWITCH",
        "AWS_BEARER_TOKEN_BEDROCK",
        "OPENROUTER_API_KEY",
        "ROBOCO_HOST_CLAUDE_DIR",
        "ROBOCO_HOST_CODEX_DIR",
        "CLAUDE_AUTH_DIR",
    ],
)
def test_inherited_auth_or_provider_override_is_rejected_by_name_only(
    name: str,
) -> None:
    with pytest.raises(SubscriptionPolicyViolation) as caught:
        validate_subscription_policy(_settings(), environ={name: SECRET_VALUE})
    assert caught.value.violations == (name,)
    assert SECRET_VALUE not in str(caught.value)
    assert SECRET_VALUE not in repr(caught.value)


def test_every_violation_is_reported_in_stable_order() -> None:
    with pytest.raises(SubscriptionPolicyViolation) as caught:
        validate_subscription_policy(
            _settings(anthropic_api_key=SECRET_VALUE),
            environ={"ANTHROPIC_BASE_URL": "https://gw.example", "A": "b"},
        )
    assert caught.value.violations == ("ROBOCO_ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")


@pytest.mark.parametrize(
    "flag", ["decisions_tier_openrouter_enabled", "token_factory_sandboxes_enabled"]
)
def test_alternate_provider_features_are_rejected(flag: str) -> None:
    with pytest.raises(SubscriptionPolicyViolation) as caught:
        validate_subscription_policy(_settings(**{flag: True}), environ={})
    assert caught.value.violations == (f"ROBOCO_{flag.upper()}",)


@pytest.mark.parametrize(
    "key",
    [
        "anthropic_api_key",
        "subscription_policy_enforced",
        "claude_auth_method",
        "default_provider",
        "openrouter_api_key",
    ],
)
def test_policy_cannot_be_overridden_by_db(key: str) -> None:
    with pytest.raises(SettingValidationError):
        validate_setting(key, "anything")


@pytest.mark.parametrize(
    "flag", ["decisions_tier_openrouter_enabled", "token_factory_sandboxes_enabled"]
)
def test_alternate_provider_flag_cannot_be_enabled_through_settings_write(
    flag: str,
) -> None:
    with pytest.raises(SettingValidationError, match="subscription"):
        validate_setting(flag, "true")
    validate_setting(flag, "false")


def test_cli_env_is_constructed_from_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_PROFILE"):
        monkeypatch.setenv(name, SECRET_VALUE)
    monkeypatch.setenv("SOME_RANDOM_HOST_VAR", "leak")
    env = build_claude_env()
    assert set(env) == {
        "PATH",
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "LANG",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_MAX_RETRIES",
    }
    assert SECRET_VALUE not in " ".join(env.values())
    assert env["HOME"] == "/home/claude"
    assert env["CLAUDE_CONFIG_DIR"] == "/home/claude/.claude"


def test_built_env_passes_its_own_policy() -> None:
    validate_subscription_policy(_settings(), environ=build_claude_env())


def test_billing_acknowledgement_accepts_only_an_iso_timestamp() -> None:
    validate_setting(
        "subscription_billing_acknowledged_at", "2026-11-20T10:15:00+00:00"
    )
    for bad in ("true", "yes", "", "2026-13-40", "not a date"):
        with pytest.raises(SettingValidationError):
            validate_setting("subscription_billing_acknowledged_at", bad)
