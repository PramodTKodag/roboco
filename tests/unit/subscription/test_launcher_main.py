"""Launcher process configuration is read from trusted files/env, never requests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from roboco.runtime.subscription.launcher_main import (
    LauncherSettingsError,
    load_launcher_settings,
)
from roboco.runtime.subscription.launcher_protocol import LauncherAction

if TYPE_CHECKING:
    from pathlib import Path

ADMIN = "admin-token-synthetic-0123456789"
EXECUTOR = "executor-token-synthetic-0123456789"


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    admin = tmp_path / "admin"
    executor = tmp_path / "executor"
    admin.write_text(ADMIN + "\n")
    executor.write_text(EXECUTOR + "\n")
    env = {
        "ROBOCO_WORKER_IMAGE": "sha256:" + "d" * 64,
        "ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE": str(admin),
        "ROBOCO_LAUNCHER_EXECUTOR_TOKEN_FILE": str(executor),
    }
    env.update(overrides)
    return env


def test_valid_environment_loads_with_safe_defaults(tmp_path: Path) -> None:
    settings = load_launcher_settings(_env(tmp_path))
    assert settings.config.image == "sha256:" + "d" * 64
    assert settings.config.runtime == "runc"
    assert settings.host == "127.0.0.1"
    assert settings.auth.allows(ADMIN, LauncherAction.REGISTER_RUN)


@pytest.mark.parametrize(
    "missing", ["ROBOCO_WORKER_IMAGE", "ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE"]
)
def test_missing_required_setting_is_refused(tmp_path: Path, missing: str) -> None:
    env = _env(tmp_path)
    del env[missing]
    with pytest.raises(LauncherSettingsError, match=missing):
        load_launcher_settings(env)


def test_unpinned_image_reference_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LauncherSettingsError, match="pinned"):
        load_launcher_settings(_env(tmp_path, ROBOCO_WORKER_IMAGE="python:latest"))


def test_unknown_runtime_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LauncherSettingsError, match="runtime"):
        load_launcher_settings(_env(tmp_path, ROBOCO_WORKER_RUNTIME="kata"))


def test_short_or_identical_tokens_are_refused_without_echoing_them(
    tmp_path: Path,
) -> None:
    short = _env(tmp_path)
    (tmp_path / "admin").write_text("tiny-secret-value")
    with pytest.raises(LauncherSettingsError) as caught:
        load_launcher_settings(short)
    assert "tiny-secret-value" not in str(caught.value)
    (tmp_path / "admin").write_text(EXECUTOR)
    with pytest.raises(LauncherSettingsError, match="distinct"):
        load_launcher_settings(short)


def test_non_loopback_bind_requires_explicit_opt_in(tmp_path: Path) -> None:
    with pytest.raises(LauncherSettingsError, match="bind"):
        load_launcher_settings(_env(tmp_path, ROBOCO_LAUNCHER_HOST="0.0.0.0"))
    allowed = load_launcher_settings(
        _env(
            tmp_path,
            ROBOCO_LAUNCHER_HOST="0.0.0.0",
            ROBOCO_LAUNCHER_ALLOW_NON_LOOPBACK="1",
        )
    )
    assert allowed.host == "0.0.0.0"
