"""Hostile-archive tests for snapshot admission (runs inside the offline worker)."""

from __future__ import annotations

import io
import tarfile
from typing import TYPE_CHECKING

import pytest
from roboco.runtime.subscription.workspace_admission import (
    AdmissionLimits,
    AdmissionRefused,
    admit_archive,
)

if TYPE_CHECKING:
    from pathlib import Path

EXECUTABLE_MODE = 0o755
REGULAR_MODE = 0o644
ANTHROPIC_KEY = "sk-ant-" + "B" * 40


def _tar(entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for info, data in entries:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    buffer.seek(0)
    return buffer


def _file(
    name: str, content: bytes = b"x", mode: int = 0o644
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    return info, content


def _special(
    name: str, kind: bytes, linkname: str = ""
) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = linkname
    return info, None


def _directory(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info, None


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path


def _fresh(base: Path, name: str) -> Path:
    path = base.parent / name
    path.mkdir()
    return path


def _refusal(archive: io.BytesIO, destination: Path, **kwargs: object) -> str:
    with pytest.raises(AdmissionRefused) as caught:
        admit_archive(archive, destination, **kwargs)  # type: ignore[arg-type]
    return caught.value.reason_code


def test_valid_archive_is_extracted_with_normalised_modes(destination: Path) -> None:
    archive = _tar(
        [
            _directory("src"),
            _file("src/app.py", b"print(1)\n"),
            _file("run.sh", b"#!/bin/sh\n", mode=0o4755),
            _file("README.md", b"hi\n", mode=0o666),
        ]
    )
    report = admit_archive(archive, destination)
    assert (destination / "src/app.py").read_text() == "print(1)\n"
    assert report.files == 3  # noqa: PLR2004
    assert (
        destination / "run.sh"
    ).stat().st_mode & 0o7777 == EXECUTABLE_MODE  # no setuid
    assert (destination / "README.md").stat().st_mode & 0o7777 == REGULAR_MODE


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "../evil", "a/../../evil", "a//b", "a/./b", "x\\y", "%2e%2e/evil"],
)
def test_escaping_or_malformed_names_refuse_the_whole_archive(
    destination: Path, name: str
) -> None:
    assert _refusal(_tar([_file("ok.txt"), _file(name)]), destination) == "path_invalid"
    assert not (destination.parent / "evil").exists()


@pytest.mark.parametrize(
    ("kind", "linkname"),
    [
        (tarfile.SYMTYPE, "/etc/passwd"),
        (tarfile.LNKTYPE, "ok.txt"),
        (tarfile.CHRTYPE, ""),
        (tarfile.BLKTYPE, ""),
        (tarfile.FIFOTYPE, ""),
    ],
)
def test_links_devices_and_fifos_are_refused(
    destination: Path, kind: bytes, linkname: str
) -> None:
    archive = _tar([_file("ok.txt"), _special("bad", kind, linkname)])
    assert _refusal(archive, destination) == "entry_type_refused"


def test_symlink_inside_a_denied_directory_is_still_refused(destination: Path) -> None:
    archive = _tar([_special(".git/hooks/x", tarfile.SYMTYPE, "/etc/shadow")])
    assert _refusal(archive, destination) == "entry_type_refused"


def test_duplicate_and_case_colliding_entries_are_refused(destination: Path) -> None:
    exact = _tar([_file("a.txt"), _file("a.txt")])
    assert _refusal(exact, destination) == "entry_duplicate"
    cased = _tar([_file("A.txt"), _file("a.txt")])
    assert _refusal(cased, _fresh(destination, "w2")) == "entry_duplicate"


def test_file_cannot_become_a_directory_prefix(destination: Path) -> None:
    archive = _tar([_file("a"), _file("a/b.txt")])
    assert _refusal(archive, destination) in {"entry_duplicate", "path_conflict"}


def test_limits_on_entries_file_size_and_total_size(destination: Path) -> None:
    limits = AdmissionLimits(max_entries=2, max_file_bytes=10, max_total_bytes=15)
    too_many = _tar([_file("a"), _file("b"), _file("c")])
    assert _refusal(too_many, destination, limits=limits) == "too_many_entries"
    big_file = _tar([_file("a", b"x" * 11)])
    fresh = _fresh(destination, "w2")
    assert _refusal(big_file, fresh, limits=limits) == "file_too_large"
    big_total = _tar([_file("a", b"x" * 9), _file("b", b"x" * 9)])
    fresh = _fresh(destination, "w3")
    assert _refusal(big_total, fresh, limits=limits) == "archive_too_large"


def test_denied_paths_are_excluded_and_reported_not_extracted(
    destination: Path,
) -> None:
    archive = _tar(
        [
            _file("keep.py", b"x = 1\n"),
            _file(".env", b"TOKEN=abc"),
            _file("deploy/server.pem", b"KEY"),
            _file(".git/config", b"[core]"),
            _file(".env.example", b"TOKEN="),
        ]
    )
    report = admit_archive(archive, destination)
    assert sorted(p.name for p in destination.rglob("*") if p.is_file()) == [
        ".env.example",
        "keep.py",
    ]
    assert set(report.excluded) == {".env", "deploy/server.pem", ".git/config"}


def test_extra_deny_globs_tighten_but_cannot_weaken(destination: Path) -> None:
    archive = _tar([_file("notes.bak"), _file(".env", b"S"), _file("a.py")])
    report = admit_archive(archive, destination, extra_deny_globs=("*.bak",))
    assert set(report.excluded) == {"notes.bak", ".env"}
    with pytest.raises(ValueError):
        admit_archive(
            _tar([_file("a.py")]),
            destination.parent / "w2",
            extra_deny_globs=("!.env",),
        )


def test_suspected_secrets_refuse_import_until_acknowledged(destination: Path) -> None:
    def archive() -> io.BytesIO:
        return _tar([_file("cfg.txt", f"key={ANTHROPIC_KEY}".encode()), _file("a.py")])

    with pytest.raises(AdmissionRefused) as caught:
        admit_archive(archive(), destination)
    assert caught.value.reason_code == "secrets_suspected"
    assert [(f.path, f.rule) for f in caught.value.findings] == [
        ("cfg.txt", "anthropic_key")
    ]
    assert ANTHROPIC_KEY not in str(caught.value) + repr(caught.value.findings)


def test_acknowledged_findings_allow_the_import(tmp_path: Path) -> None:
    destination = tmp_path / "ws2"
    destination.mkdir()
    archive = _tar([_file("cfg.txt", f"key={ANTHROPIC_KEY}".encode())])
    report = admit_archive(
        archive, destination, allowed_findings=frozenset({"cfg.txt:anthropic_key"})
    )
    assert report.files == 1


def test_non_empty_destination_empty_archive_and_garbage_are_refused(
    destination: Path,
) -> None:
    (destination / "old.txt").write_text("x")
    assert _refusal(_tar([_file("a")]), destination) == "workspace_not_empty"
    empty_dir = destination.parent / "empty"
    empty_dir.mkdir()
    assert _refusal(_tar([]), empty_dir) == "archive_empty"
    other = destination.parent / "other"
    other.mkdir()
    assert _refusal(io.BytesIO(b"not a tar" * 100), other) == "archive_malformed"


def test_compressed_archives_are_refused_to_rule_out_decompression_bombs(
    destination: Path,
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("a")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    buffer.seek(0)
    assert _refusal(buffer, destination) == "archive_malformed"


def test_directory_entries_inside_denied_trees_do_not_create_them(
    destination: Path,
) -> None:
    archive = _tar([_directory(".ssh"), _file("a.py")])
    admit_archive(archive, destination)
    assert not (destination / ".ssh").exists()
