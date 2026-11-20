"""``_register_tools`` raises on a missing manifest, with no override.

The manifest is the role-authoritative tool list; a server without it must
refuse to start rather than expose off-role verbs. No environment variable
relaxes this, including the retired ``ROBOCO_ALLOW_FULL_TOOLSET``.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _seed_no_manifest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ROBOCO_AGENT_ID", "00000000-0000-0000-0000-000000000099")
    monkeypatch.setenv("ROBOCO_AGENT_ROLE", "developer")
    monkeypatch.setenv("ROBOCO_ORCHESTRATOR_URL", "http://test-orchestrator:8000")
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("ROBOCO_TOOL_MANIFEST_PATH", str(missing))


@pytest.mark.parametrize("override", [None, "1"])
def test_flow_missing_manifest_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str | None
) -> None:
    _seed_no_manifest(monkeypatch, tmp_path)
    if override is not None:
        monkeypatch.setenv("ROBOCO_ALLOW_FULL_TOOLSET", override)
    import roboco.mcp.flow_server as srv

    with pytest.raises(RuntimeError, match="manifest unavailable"):
        importlib.reload(srv)


@pytest.mark.parametrize("override", [None, "1"])
def test_do_missing_manifest_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, override: str | None
) -> None:
    _seed_no_manifest(monkeypatch, tmp_path)
    if override is not None:
        monkeypatch.setenv("ROBOCO_ALLOW_FULL_TOOLSET", override)
    import roboco.mcp.do_server as srv

    with pytest.raises(RuntimeError, match="manifest unavailable"):
        importlib.reload(srv)
