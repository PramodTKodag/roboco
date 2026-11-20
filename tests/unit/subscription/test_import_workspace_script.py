"""The owner import script: dry-run output and argument handling."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).parents[3] / "scripts" / "personal" / "import_workspace.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_workspace_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_dry_run_prints_the_omissions_and_never_contacts_the_launcher(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / ".env").write_text("TOKEN=synthetic")
    monkeypatch.delenv("ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE", raising=False)
    module = _load()
    code = module.main(
        [
            "--source",
            str(tmp_path),
            "--project-id",
            "00000000-0000-4000-8000-000000000001",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "snapshot: 1 files" in out
    assert "omitted .env: denied" in out
    assert "synthetic" not in out


def test_unusable_source_exits_with_usage_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _load().main(
        [
            "--source",
            str(tmp_path / "missing"),
            "--project-id",
            "00000000-0000-4000-8000-000000000001",
            "--dry-run",
        ]
    )
    assert code == 2  # noqa: PLR2004
    assert "snapshot failed" in capsys.readouterr().err


def test_live_import_requires_the_admin_token_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    monkeypatch.delenv("ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit):
        _load().main(
            [
                "--source",
                str(tmp_path),
                "--project-id",
                "00000000-0000-4000-8000-000000000001",
            ]
        )
