"""Policy is enforced at process start and on persisted flag rows."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from roboco import cli
from roboco.config import settings
from roboco.services import settings as settings_service

POLICY_EXIT_CODE = 2


def test_cli_refuses_to_start_with_an_inherited_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SYNTHETIC-never-echoed")
    monkeypatch.setattr(sys, "argv", ["roboco"])
    started: list[bool] = []
    monkeypatch.setattr(cli, "main", lambda **_: started.append(True))
    with pytest.raises(SystemExit) as exit_info:
        cli.cli()
    assert exit_info.value.code == POLICY_EXIT_CODE
    assert not started
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert "sk-ant-SYNTHETIC-never-echoed" not in err


class _StoredFlags:
    """Stand-in for SettingsService holding persisted rows."""

    def __init__(self, rows: dict[str, str]) -> None:
        self._rows = rows

    async def get(self, key: str) -> str | None:
        return self._rows.get(key)

    async def get_bool(self, key: str, default: bool) -> bool:
        raw = self._rows.get(key)
        return default if raw is None else raw.strip().lower() == "true"


async def test_persisted_forbidden_flag_row_is_never_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _StoredFlags(
        {
            "decisions_tier_openrouter_enabled": "true",
            "token_factory_sandboxes_enabled": "true",
            "research_enabled": "false",
        }
    )
    monkeypatch.setattr(settings_service, "get_settings_service", lambda _s: stored)
    monkeypatch.setattr(settings, "decisions_tier_openrouter_enabled", False)
    monkeypatch.setattr(settings, "token_factory_sandboxes_enabled", False)
    session: Any = object()

    applied = await settings_service.apply_persisted_feature_flags(session)
    effective = await settings_service.feature_flag_effective_values(session)

    assert "decisions_tier_openrouter_enabled" not in applied
    assert settings.decisions_tier_openrouter_enabled is False
    assert settings.token_factory_sandboxes_enabled is False
    assert effective["decisions_tier_openrouter_enabled"] is False
    assert effective["token_factory_sandboxes_enabled"] is False
