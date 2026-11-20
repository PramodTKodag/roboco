"""Descriptor-relative, no-follow workspace file operations (incl. races)."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import pytest
from roboco.runtime.subscription.contracts import MAX_FILE_BYTES, MAX_STREAM_BYTES
from roboco.runtime.subscription.worker_files import (
    WorkspaceFileError,
    list_files,
    read_file,
    search_text,
    write_file,
)

if TYPE_CHECKING:
    from pathlib import Path


def _reason(excinfo: pytest.ExceptionInfo[WorkspaceFileError]) -> str:
    return excinfo.value.reason_code


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('hi')\nneedle here\n")
    (root / "README.md").write_text("docs\n")
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "secret.txt").write_text("PROVIDER-SENTINEL")
    return directory


def test_read_returns_text_and_size(workspace: Path) -> None:
    out = read_file(workspace, "src/a.py", 0)
    assert out.text == "print('hi')\nneedle here\n"
    assert out.truncated is False


def test_read_pages_with_offset_and_marks_truncation(workspace: Path) -> None:
    big = "x" * (MAX_STREAM_BYTES + 10)
    (workspace / "big.txt").write_text(big)
    first = read_file(workspace, "big.txt", 0)
    assert first.truncated is True
    assert len(first.text.encode()) == MAX_STREAM_BYTES
    rest = read_file(workspace, "big.txt", MAX_STREAM_BYTES)
    assert rest.text == "x" * 10
    assert rest.truncated is False


def test_read_refuses_files_over_the_limit(workspace: Path) -> None:
    (workspace / "huge.bin").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(WorkspaceFileError) as caught:
        read_file(workspace, "huge.bin", 0)
    assert _reason(caught) == "file_too_large"


def test_missing_file_and_directory_reads_are_structured_errors(
    workspace: Path,
) -> None:
    with pytest.raises(WorkspaceFileError) as missing:
        read_file(workspace, "nope.txt", 0)
    assert _reason(missing) == "not_found"
    with pytest.raises(WorkspaceFileError) as directory:
        read_file(workspace, "src", 0)
    assert _reason(directory) == "not_regular"


def test_symlink_final_object_is_refused(workspace: Path, outside: Path) -> None:
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(WorkspaceFileError) as caught:
        read_file(workspace, "link.txt", 0)
    assert _reason(caught) == "symlink_refused"


def test_symlinked_parent_directory_is_refused(workspace: Path, outside: Path) -> None:
    (workspace / "linkdir").symlink_to(outside)
    with pytest.raises(WorkspaceFileError) as caught:
        read_file(workspace, "linkdir/secret.txt", 0)
    assert _reason(caught) == "symlink_refused"
    with pytest.raises(WorkspaceFileError):
        write_file(workspace, "linkdir/new.txt", "x")
    assert not (outside / "new.txt").exists()


def test_hardlinked_file_is_refused(workspace: Path, outside: Path) -> None:
    os.link(outside / "secret.txt", workspace / "hard.txt")
    with pytest.raises(WorkspaceFileError) as caught:
        read_file(workspace, "hard.txt", 0)
    assert _reason(caught) == "hardlink_refused"
    with pytest.raises(WorkspaceFileError):
        write_file(workspace, "hard.txt", "overwrite")
    assert (outside / "secret.txt").read_text() == "PROVIDER-SENTINEL"


def test_fifo_is_refused_without_blocking(workspace: Path) -> None:
    os.mkfifo(workspace / "pipe")
    with pytest.raises(WorkspaceFileError) as caught:
        read_file(workspace, "pipe", 0)
    assert _reason(caught) == "not_regular"


@pytest.mark.parametrize(
    "path", [".env", "app/.env.production", ".git/config", "k.pem"]
)
def test_denied_paths_cannot_be_read_or_written(workspace: Path, path: str) -> None:
    target = workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET")
    with pytest.raises(WorkspaceFileError) as read:
        read_file(workspace, path, 0)
    assert _reason(read) == "path_denied"
    with pytest.raises(WorkspaceFileError) as write:
        write_file(workspace, path, "new")
    assert _reason(write) == "path_denied"
    assert target.read_text() == "SECRET"


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a//b", "src/../../x"])
def test_escaping_paths_are_refused(workspace: Path, path: str) -> None:
    with pytest.raises(WorkspaceFileError) as caught:
        read_file(workspace, path, 0)
    assert _reason(caught) == "path_invalid"


def test_write_creates_parents_and_replaces_atomically(workspace: Path) -> None:
    write_file(workspace, "new/deep/file.txt", "one")
    write_file(workspace, "new/deep/file.txt", "two")
    assert (workspace / "new/deep/file.txt").read_text() == "two"
    leftovers = [p.name for p in (workspace / "new/deep").iterdir()]
    assert leftovers == ["file.txt"]


def test_write_refuses_oversized_content_and_directory_targets(
    workspace: Path,
) -> None:
    with pytest.raises(WorkspaceFileError) as big:
        write_file(workspace, "big.txt", "a" * (MAX_FILE_BYTES + 1))
    assert _reason(big) == "file_too_large"
    assert not (workspace / "big.txt").exists()
    with pytest.raises(WorkspaceFileError) as directory:
        write_file(workspace, "src", "x")
    assert _reason(directory) == "not_regular"


def test_write_replaces_a_symlink_name_without_following_it(
    workspace: Path, outside: Path
) -> None:
    (workspace / "ln.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(WorkspaceFileError) as caught:
        write_file(workspace, "ln.txt", "x")
    assert _reason(caught) == "symlink_refused"
    assert (outside / "secret.txt").read_text() == "PROVIDER-SENTINEL"


def test_list_hides_denied_entries_and_non_regular_objects(
    workspace: Path, outside: Path
) -> None:
    (workspace / ".env").write_text("S")
    (workspace / "lnk").symlink_to(outside)
    os.mkfifo(workspace / "pipe")
    listing = list_files(workspace, ".")
    assert listing.text.split("\n") == ["README.md", "src/"]


def test_list_of_a_subdirectory_and_missing_path(workspace: Path) -> None:
    assert list_files(workspace, "src").text.split("\n") == ["a.py"]
    with pytest.raises(WorkspaceFileError) as caught:
        list_files(workspace, "absent")
    assert _reason(caught) == "not_found"


def test_search_finds_literal_text_with_line_numbers(workspace: Path) -> None:
    out = search_text(workspace, "needle", ".")
    assert out.text == "src/a.py:2:needle here"
    assert out.truncated is False


def test_search_is_literal_not_regex(workspace: Path) -> None:
    (workspace / "r.txt").write_text("a.b\naxb\n")
    assert search_text(workspace, "a.b", "r.txt").text == "r.txt:1:a.b"


def test_search_skips_denied_symlinked_and_binary_files(
    workspace: Path, outside: Path
) -> None:
    (workspace / ".env").write_text("needle")
    (workspace / "lnk").symlink_to(outside)
    (outside / "x.txt").write_text("needle")
    (workspace / "bin.dat").write_bytes(b"needle\xff\xfe")
    hits = search_text(workspace, "needle", ".").text.split("\n")
    assert hits == ["src/a.py:2:needle here"]


def test_search_caps_matches_and_says_so(workspace: Path) -> None:
    (workspace / "many.txt").write_text("hit\n" * 1000)
    out = search_text(workspace, "hit", "many.txt")
    assert out.truncated is True
    assert len(out.text.splitlines()) <= 200  # noqa: PLR2004


def test_concurrent_parent_swap_never_reads_outside_the_workspace(
    workspace: Path, outside: Path
) -> None:
    (workspace / "d").mkdir()
    (workspace / "d" / "secret.txt").write_text("INSIDE")
    (outside / "d_target").mkdir()
    (outside / "d_target" / "secret.txt").write_text("PROVIDER-SENTINEL")
    stop = threading.Event()

    def swapper() -> None:
        while not stop.is_set():
            try:
                (workspace / "d").rename(workspace / "d_real")
                (workspace / "d").symlink_to(outside / "d_target")
                (workspace / "d").unlink()
                (workspace / "d_real").rename(workspace / "d")
            except OSError:
                continue

    thread = threading.Thread(target=swapper, daemon=True)
    thread.start()
    leaked = False
    try:
        for _ in range(3000):
            try:
                text = read_file(workspace, "d/secret.txt", 0).text
            except WorkspaceFileError:
                continue
            leaked = leaked or "PROVIDER-SENTINEL" in text
    finally:
        stop.set()
        thread.join(timeout=5)
    assert leaked is False


def test_concurrent_parent_swap_never_writes_outside_the_workspace(
    workspace: Path, outside: Path
) -> None:
    (workspace / "d").mkdir()
    (outside / "d_target").mkdir()
    stop = threading.Event()

    def swapper() -> None:
        while not stop.is_set():
            try:
                (workspace / "d").rename(workspace / "d_real")
                (workspace / "d").symlink_to(outside / "d_target")
                (workspace / "d").unlink()
                (workspace / "d_real").rename(workspace / "d")
            except OSError:
                continue

    thread = threading.Thread(target=swapper, daemon=True)
    thread.start()
    try:
        for index in range(1500):
            try:
                write_file(workspace, f"d/out{index % 7}.txt", "payload")
            except WorkspaceFileError:
                continue
    finally:
        stop.set()
        thread.join(timeout=5)
    assert list((outside / "d_target").iterdir()) == []
