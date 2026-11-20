"""The service refuses to start without a valid trusted egress route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from roboco.runtime.subscription.service_main import (
    ServiceSettingsError,
    load_service_settings,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    service_token = tmp_path / "service-token"
    executor_token = tmp_path / "executor-token"
    ca_file = tmp_path / "ca.pem"
    service_token.write_text("service-token-for-tests")
    executor_token.write_text("executor-token-for-tests")
    ca_file.write_text("test ca placeholder")
    return {
        "ROBOCO_CLAUDE_SERVICE_TOKEN_FILE": str(service_token),
        "ROBOCO_LAUNCHER_HOST": "launcher",
        "ROBOCO_LAUNCHER_PORT": "8765",
        "ROBOCO_LAUNCHER_EXECUTOR_TOKEN_FILE": str(executor_token),
        "ROBOCO_EGRESS_PROXY_URL": "http://egress-proxy:8080",
        "ROBOCO_EGRESS_CA_FILE": str(ca_file),
    }


def test_valid_egress_route_is_loaded(base_env: dict[str, str]) -> None:
    settings = load_service_settings(base_env)
    assert settings.egress.proxy_url == "http://egress-proxy:8080"
    assert settings.egress.ca_file == base_env["ROBOCO_EGRESS_CA_FILE"]


@pytest.mark.parametrize(
    "missing", ["ROBOCO_EGRESS_PROXY_URL", "ROBOCO_EGRESS_CA_FILE"]
)
def test_a_missing_egress_setting_refuses_startup(
    base_env: dict[str, str], missing: str
) -> None:
    del base_env[missing]
    with pytest.raises(ServiceSettingsError, match=missing):
        load_service_settings(base_env)


def test_an_unreadable_ca_refuses_startup(
    base_env: dict[str, str], tmp_path: Path
) -> None:
    base_env["ROBOCO_EGRESS_CA_FILE"] = str(tmp_path / "absent.pem")
    with pytest.raises(ServiceSettingsError, match="unreadable"):
        load_service_settings(base_env)


def test_a_malformed_proxy_url_refuses_startup(base_env: dict[str, str]) -> None:
    base_env["ROBOCO_EGRESS_PROXY_URL"] = "https://user:pw@egress-proxy:8080/x"
    with pytest.raises(ServiceSettingsError, match="egress route refused"):
        load_service_settings(base_env)
