"""Safe file operations beneath one workspace root.

Every path segment is opened relative to the previous directory descriptor with
``O_NOFOLLOW``, so a concurrent swap of any component for a symlink cannot move
an operation outside the workspace (a resolve-then-open check could). Only
regular, singly-linked files and plain directories are ever touched. Runs only
inside the credential-free worker.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from roboco.runtime.subscription.contracts import MAX_FILE_BYTES, MAX_STREAM_BYTES
from roboco.runtime.subscription.workspace_rules import (
    InvalidWorkspacePath,
    is_denied_path,
    split_archive_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW
MAX_LIST_ENTRIES = 2000
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILES = 5000
MAX_MATCH_LINE_CHARS = 300
_TEMP_PREFIX = ".roboco-write-"


class WorkspaceFileError(Exception):
    """A refused or failed file operation. Carries a stable reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class FileOutput:
    text: str
    truncated: bool = False


def _segments_of(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if allow_root and path == ".":
        return ()
    try:
        segments = split_archive_path(path)
    except InvalidWorkspacePath as exc:
        raise WorkspaceFileError("path_invalid") from exc
    if is_denied_path(path):
        raise WorkspaceFileError("path_denied")
    return segments


def _refusal_for(
    dir_fd: int, name: str, error: OSError, *, want_dir: bool
) -> Exception:
    """Map a failed no-follow open to a stable reason by looking at the entry."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return WorkspaceFileError("not_found")
    if stat.S_ISLNK(info.st_mode):
        return WorkspaceFileError("symlink_refused")
    if want_dir:
        return WorkspaceFileError("not_a_directory")
    if stat.S_ISDIR(info.st_mode):
        return WorkspaceFileError("not_regular")
    if error.errno in {errno.ENOENT}:
        return WorkspaceFileError("not_found")
    return WorkspaceFileError("not_regular")


def _open_dir(parent_fd: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise WorkspaceFileError("not_found") from None
    except OSError as exc:
        raise _refusal_for(parent_fd, name, exc, want_dir=True) from exc
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _refusal_for(parent_fd, name, exc, want_dir=True) from exc


@contextlib.contextmanager
def _walk(
    root: Path, segments: tuple[str, ...], *, create: bool = False
) -> Iterator[int]:
    """Yield an fd for the directory at ``segments`` beneath ``root``."""
    opened: list[int] = []
    try:
        try:
            current = os.open(root, _DIR_FLAGS)
        except OSError as exc:
            raise WorkspaceFileError("workspace_unavailable") from exc
        opened.append(current)
        for name in segments:
            current = _open_dir(current, name, create=create)
            opened.append(current)
        yield current
    finally:
        for descriptor in reversed(opened):
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _open_regular(dir_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise WorkspaceFileError("not_found") from None
    except OSError as exc:
        raise _refusal_for(dir_fd, name, exc, want_dir=False) from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise WorkspaceFileError("not_regular")
    if info.st_nlink > 1:
        os.close(descriptor)
        raise WorkspaceFileError("hardlink_refused")
    return descriptor


def read_file(root: Path, path: str, offset: int) -> FileOutput:
    segments = _segments_of(path)
    with _walk(root, segments[:-1]) as parent:
        descriptor = _open_regular(parent, segments[-1])
    try:
        if os.fstat(descriptor).st_size > MAX_FILE_BYTES:
            raise WorkspaceFileError("file_too_large")
        data = os.pread(descriptor, MAX_STREAM_BYTES + 1, offset)
    finally:
        os.close(descriptor)
    truncated = len(data) > MAX_STREAM_BYTES
    return FileOutput(
        data[:MAX_STREAM_BYTES].decode("utf-8", errors="replace"), truncated
    )


def _listing_entries(dir_fd: int, prefix: str) -> list[str]:
    names: list[str] = []
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            if is_denied_path(relative):
                continue
            if entry.is_dir(follow_symlinks=False):
                names.append(f"{entry.name}/")
            elif entry.is_file(follow_symlinks=False):
                names.append(entry.name)
    return sorted(names)


def list_files(root: Path, path: str) -> FileOutput:
    segments = _segments_of(path, allow_root=True)
    prefix = "/".join(segments) + "/" if segments else ""
    with _walk(root, segments) as directory:
        names = _listing_entries(directory, prefix)
    truncated = len(names) > MAX_LIST_ENTRIES
    return FileOutput("\n".join(names[:MAX_LIST_ENTRIES]), truncated)


@dataclass
class _SearchState:
    pattern: str
    hits: list[str] = field(default_factory=list)
    files_seen: int = 0
    truncated: bool = False

    @property
    def done(self) -> bool:
        return self.truncated


def _search_file(dir_fd: int, name: str, relative: str, state: _SearchState) -> None:
    if state.files_seen >= MAX_SEARCH_FILES:
        state.truncated = True
        return
    state.files_seen += 1
    try:
        descriptor = _open_regular(dir_fd, name)
    except WorkspaceFileError:
        return
    try:
        if os.fstat(descriptor).st_size > MAX_FILE_BYTES:
            return
        raw = os.pread(descriptor, MAX_FILE_BYTES, 0)
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    for number, line in enumerate(text.splitlines(), start=1):
        if state.pattern not in line:
            continue
        if len(state.hits) >= MAX_SEARCH_MATCHES:
            state.truncated = True
            return
        state.hits.append(f"{relative}:{number}:{line[:MAX_MATCH_LINE_CHARS]}")


def _search_directory(dir_fd: int, prefix: str, state: _SearchState) -> None:
    with os.scandir(dir_fd) as scanned:
        entries = sorted(scanned, key=lambda e: e.name)
        for entry in entries:
            if state.done:
                return
            relative = f"{prefix}{entry.name}"
            if is_denied_path(relative):
                continue
            if entry.is_dir(follow_symlinks=False):
                try:
                    child = os.open(entry.name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError:
                    continue
                try:
                    _search_directory(child, f"{relative}/", state)
                finally:
                    os.close(child)
            elif entry.is_file(follow_symlinks=False):
                _search_file(dir_fd, entry.name, relative, state)


def search_text(root: Path, pattern: str, path: str) -> FileOutput:
    """Literal (never regex) substring search under ``path``."""
    segments = _segments_of(path, allow_root=True)
    state = _SearchState(pattern=pattern)
    prefix = "/".join(segments) + "/" if segments else ""
    parent_segments = segments[:-1] if segments else ()
    with _walk(root, parent_segments) as parent:
        if not segments:
            _search_directory(parent, "", state)
        else:
            _search_target(parent, segments[-1], prefix.rstrip("/"), state)
    return FileOutput("\n".join(state.hits), state.truncated)


def _search_target(parent: int, name: str, relative: str, state: _SearchState) -> None:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except OSError as exc:
        raise WorkspaceFileError("not_found") from exc
    if stat.S_ISDIR(info.st_mode):
        child = _open_dir(parent, name, create=False)
        try:
            _search_directory(child, f"{relative}/", state)
        finally:
            os.close(child)
    elif stat.S_ISREG(info.st_mode):
        _search_file(parent, name, relative, state)
    else:
        raise WorkspaceFileError(
            "symlink_refused" if stat.S_ISLNK(info.st_mode) else "not_regular"
        )


def _check_replaceable(dir_fd: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceFileError("symlink_refused")
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceFileError("not_regular")
    if info.st_nlink > 1:
        raise WorkspaceFileError("hardlink_refused")


def _replace_atomically(parent: int, name: str, data: bytes) -> None:
    temporary = f"{_TEMP_PREFIX}{secrets.token_hex(8)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
        0o644,
        dir_fd=parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=parent)
        raise


def write_file(root: Path, path: str, content: str) -> None:
    """Create or atomically replace one regular file; never follows a link."""
    segments = _segments_of(path)
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise WorkspaceFileError("file_too_large")
    try:
        with _walk(root, segments[:-1], create=True) as parent:
            _check_replaceable(parent, segments[-1])
            _replace_atomically(parent, segments[-1], data)
    except OSError as exc:
        # The tree changed underneath the operation (or the disk refused it).
        raise WorkspaceFileError("workspace_unavailable") from exc


def write_new_file(
    root: Path, segments: tuple[str, ...], data: bytes, *, executable: bool
) -> None:
    """Create one new regular file (exclusive, no-follow); used by import only."""
    mode = 0o755 if executable else 0o644
    try:
        with _walk(root, segments[:-1], create=True) as parent:
            descriptor = os.open(
                segments[-1],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW,
                mode,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                os.fchmod(handle.fileno(), mode)
    except OSError as exc:
        raise WorkspaceFileError("workspace_unavailable") from exc


def make_directory(root: Path, segments: tuple[str, ...]) -> None:
    """Create a plain directory chain beneath the workspace (no-follow)."""
    with _walk(root, segments, create=True):
        pass
