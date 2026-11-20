"""Owner-side snapshot of a local project directory for import into a worker.

Runs in the trusted controller on a directory the owner chose. It never follows
links, never reads special files, applies the same fixed deny rules as the
worker (plus any extra globs the owner adds), and records every omission so the
owner can review exactly what was left out. The worker re-validates the archive
independently; nothing here is trusted by it.
"""

from __future__ import annotations

import io
import logging
import os
import stat
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from roboco.runtime.subscription.workspace_admission import AdmissionLimits
from roboco.runtime.subscription.workspace_rules import (
    InvalidWorkspacePath,
    is_denied_path,
    tighten_deny_globs,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_EXECUTABLE_BITS = 0o111


class SnapshotError(Exception):
    """The source directory cannot be snapshotted. Message carries no file content."""


@dataclass
class SnapshotResult:
    archive: bytes
    files: int = 0
    total_bytes: int = 0
    omitted: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _Builder:
    root: Path
    limits: AdmissionLimits
    extra_globs: tuple[str, ...]
    tar: tarfile.TarFile
    result: SnapshotResult
    entries: int = 0


def _omit(builder: _Builder, relative: str, reason: str) -> None:
    builder.result.omitted.append((relative, reason))


def _read_regular_file(path: Path, expected_size: int) -> tuple[bytes, bool] | None:
    """Read (content, is_executable) without following links or blocking."""
    flags = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
            return None
        return handle.read(expected_size + 1), bool(info.st_mode & _EXECUTABLE_BITS)


def _add_file(builder: _Builder, path: Path, relative: str, size: int) -> None:
    if size > builder.limits.max_file_bytes:
        _omit(builder, relative, "file_too_large")
        return
    if builder.result.total_bytes + size > builder.limits.max_total_bytes:
        raise SnapshotError("snapshot exceeds the total size limit")
    read = _read_regular_file(path, size)
    if read is None or len(read[0]) != size:
        _omit(builder, relative, "changed_or_unreadable")
        return
    data, executable = read
    info = tarfile.TarInfo(relative)
    info.size = size
    info.mode = 0o755 if executable else 0o644
    builder.tar.addfile(info, io.BytesIO(data))
    builder.result.files += 1
    builder.result.total_bytes += size


def _classify(builder: _Builder, directory: Path, entry: os.DirEntry[str]) -> None:
    relative = (directory / entry.name).relative_to(builder.root).as_posix()
    try:
        info = entry.stat(follow_symlinks=False)
        denied = is_denied_path(relative, builder.extra_globs)
    except (OSError, InvalidWorkspacePath):
        _omit(builder, relative, "unreadable_or_invalid_name")
        return
    if stat.S_ISLNK(info.st_mode):
        _omit(builder, relative, "symlink")
    elif denied:
        _omit(builder, relative, "denied")
    elif stat.S_ISDIR(info.st_mode):
        _walk(builder, directory / entry.name)
    elif stat.S_ISREG(info.st_mode):
        builder.entries += 1
        if builder.entries > builder.limits.max_entries:
            raise SnapshotError("snapshot exceeds the entry limit")
        _add_file(builder, directory / entry.name, relative, info.st_size)
    else:
        _omit(builder, relative, "not_regular_file")


def _walk(builder: _Builder, directory: Path) -> None:
    with os.scandir(directory) as scanner:
        for entry in sorted(scanner, key=lambda item: item.name):
            _classify(builder, directory, entry)


def build_snapshot(
    source: Path,
    *,
    extra_exclude_globs: tuple[str, ...] = (),
    limits: AdmissionLimits | None = None,
) -> SnapshotResult:
    """Tar ``source`` (a real directory, not a link) into an uncompressed archive."""
    if source.is_symlink() or not source.is_dir():
        raise SnapshotError("source must be a real directory, not a link")
    try:
        globs = tighten_deny_globs(extra_exclude_globs)
    except ValueError as exc:
        raise SnapshotError("exclude glob is malformed") from exc
    buffer = io.BytesIO()
    result = SnapshotResult(archive=b"")
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        builder = _Builder(
            root=source.resolve(),
            limits=limits or AdmissionLimits(),
            extra_globs=globs,
            tar=archive,
            result=result,
        )
        _walk(builder, builder.root)
    result.archive = buffer.getvalue()
    if result.files == 0:
        raise SnapshotError("nothing to import after exclusions")
    logger.info(
        "Snapshot built: %d files, %d omitted", result.files, len(result.omitted)
    )
    return result
