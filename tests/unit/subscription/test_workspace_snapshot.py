"""Owner-side snapshot builder against hostile source trees."""

from __future__ import annotations

import io
import os
import tarfile
from typing import TYPE_CHECKING

import pytest
from roboco.runtime.subscription.workspace_admission import (
    AdmissionLimits,
    admit_archive,
)
from roboco.runtime.workspace_snapshot import SnapshotError, build_snapshot

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print(1)\n")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    return root


def _names(archive: bytes) -> set[str]:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        return {m.name for m in tar.getmembers()}


def _omitted(result_omitted: list[tuple[str, str]]) -> dict[str, str]:
    return dict(result_omitted)


def test_builds_a_plain_archive_the_worker_side_accepts(
    source: Path, tmp_path: Path
) -> None:
    result = build_snapshot(source)
    assert _names(result.archive) == {"src/app.py", "run.sh"}
    destination = tmp_path / "ws"
    destination.mkdir()
    report = admit_archive(io.BytesIO(result.archive), destination)
    assert report.files == result.files == 2  # noqa: PLR2004
    assert os.access(destination / "run.sh", os.X_OK)


def test_symlinks_special_files_and_denied_paths_are_omitted_with_reasons(
    source: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (source / "link").symlink_to(outside)
    (source / "dirlink").symlink_to(tmp_path)
    os.mkfifo(source / "pipe")
    (source / ".env").write_text("TOKEN=x")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("[core]")
    (source / "certs").mkdir()
    (source / "certs" / "server.pem").write_text("pem")
    result = build_snapshot(source)
    assert _names(result.archive) == {"src/app.py", "run.sh"}
    assert _omitted(result.omitted) == {
        "link": "symlink",
        "dirlink": "symlink",
        "pipe": "not_regular_file",
        ".env": "denied",
        ".git": "denied",
        "certs/server.pem": "denied",
    }


def test_extra_excludes_apply_but_cannot_weaken_the_fixed_rules(source: Path) -> None:
    (source / "notes.bak").write_text("n")
    (source / ".env").write_text("S=1")
    result = build_snapshot(source, extra_exclude_globs=("*.bak",))
    assert "notes.bak" not in _names(result.archive)
    assert ".env" not in _names(result.archive)
    with pytest.raises(SnapshotError):
        build_snapshot(source, extra_exclude_globs=("!.env",))


def test_source_must_be_a_real_directory(source: Path, tmp_path: Path) -> None:
    link = tmp_path / "linked"
    link.symlink_to(source)
    with pytest.raises(SnapshotError):
        build_snapshot(link)
    with pytest.raises(SnapshotError):
        build_snapshot(source / "run.sh")
    with pytest.raises(SnapshotError):
        build_snapshot(tmp_path / "missing")


def test_oversize_files_are_omitted_and_total_cap_is_a_hard_stop(
    source: Path,
) -> None:
    (source / "big.bin").write_bytes(b"x" * 100)
    limits = AdmissionLimits(max_entries=10, max_file_bytes=50, max_total_bytes=1000)
    result = build_snapshot(source, limits=limits)
    assert _omitted(result.omitted)["big.bin"] == "file_too_large"
    with pytest.raises(SnapshotError):
        build_snapshot(
            source,
            limits=AdmissionLimits(
                max_entries=10, max_file_bytes=50, max_total_bytes=5
            ),
        )
    with pytest.raises(SnapshotError):
        build_snapshot(
            source,
            limits=AdmissionLimits(
                max_entries=1, max_file_bytes=50, max_total_bytes=1000
            ),
        )


def test_empty_result_after_exclusions_is_an_error(tmp_path: Path) -> None:
    root = tmp_path / "only-secrets"
    root.mkdir()
    (root / ".env").write_text("S=1")
    with pytest.raises(SnapshotError):
        build_snapshot(root)


def test_source_files_are_not_modified_and_modes_are_normalised(source: Path) -> None:
    (source / "setuid").write_text("x")
    (source / "setuid").chmod(0o4755)
    result = build_snapshot(source)
    with tarfile.open(fileobj=io.BytesIO(result.archive)) as tar:
        modes = {m.name: m.mode for m in tar.getmembers()}
    assert modes["setuid"] == 0o755  # noqa: PLR2004
    assert modes["src/app.py"] == 0o644  # noqa: PLR2004
