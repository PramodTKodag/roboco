"""Admission of an owner-supplied snapshot archive into an empty workspace.

Runs only inside the offline worker, so hostile archive bytes are parsed in the
jail and never in a trusted component. The archive is a plain (uncompressed) tar
read as a stream: only regular files and directories are admitted, anything
that could name a place outside the workspace refuses the whole archive, and
platform-denied credential/config paths are excluded and reported. Suspected
secrets refuse the import until the owner acknowledges each exact finding.
"""

from __future__ import annotations

import logging
import tarfile
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO

from roboco.runtime.subscription.contracts import MAX_IMPORT_REPORT_ITEMS
from roboco.runtime.subscription.worker_files import (
    WorkspaceFileError,
    make_directory,
    write_new_file,
)
from roboco.runtime.subscription.workspace_rules import (
    InvalidWorkspacePath,
    SecretFinding,
    is_denied_path,
    scan_for_secrets,
    split_archive_path,
    tighten_deny_globs,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("roboco.worker.admission")

_REGULAR_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE)


@dataclass(frozen=True)
class AdmissionLimits:
    max_entries: int = 50_000
    max_file_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024


class AdmissionRefused(Exception):
    """The archive was refused. Never carries file content or secret values."""

    def __init__(
        self, reason_code: str, findings: tuple[SecretFinding, ...] = ()
    ) -> None:
        self.reason_code = reason_code
        self.findings = findings
        super().__init__(reason_code)


@dataclass
class AdmissionReport:
    files: int = 0
    directories: int = 0
    total_bytes: int = 0
    excluded: list[str] = field(default_factory=list)


@dataclass
class _Walk:
    destination: Path
    limits: AdmissionLimits
    extra_deny_globs: tuple[str, ...]
    allowed_findings: frozenset[str]
    report: AdmissionReport = field(default_factory=AdmissionReport)
    seen: set[str] = field(default_factory=set)
    seen_files: set[str] = field(default_factory=set)
    entries: int = 0
    findings: list[SecretFinding] = field(default_factory=list)


def _register(state: _Walk, segments: tuple[str, ...], *, is_file: bool) -> None:
    key = "/".join(segments).casefold()
    if key in state.seen:
        raise AdmissionRefused("entry_duplicate")
    prefixes = {"/".join(segments[:n]).casefold() for n in range(1, len(segments))}
    if prefixes & state.seen_files:
        raise AdmissionRefused("path_conflict")
    state.seen.add(key)
    if is_file:
        state.seen_files.add(key)


def _admit_member(
    state: _Walk, archive: tarfile.TarFile, member: tarfile.TarInfo
) -> None:
    state.entries += 1
    if state.entries > state.limits.max_entries:
        raise AdmissionRefused("too_many_entries")
    is_file = member.type in _REGULAR_TYPES
    if not (is_file or member.type == tarfile.DIRTYPE):
        raise AdmissionRefused("entry_type_refused")
    try:
        segments = split_archive_path(member.name)
    except InvalidWorkspacePath as exc:
        raise AdmissionRefused("path_invalid") from exc
    _register(state, segments, is_file=is_file)
    relative = "/".join(segments)
    if is_denied_path(relative, state.extra_deny_globs):
        if len(state.report.excluded) < MAX_IMPORT_REPORT_ITEMS:
            state.report.excluded.append(relative)
        return
    try:
        if not is_file:
            make_directory(state.destination, segments)
            state.report.directories += 1
            return
        _admit_file(state, archive, member, segments, relative)
    except WorkspaceFileError as exc:
        raise AdmissionRefused("path_conflict") from exc


def _admit_file(
    state: _Walk,
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    segments: tuple[str, ...],
    relative: str,
) -> None:
    if member.size > state.limits.max_file_bytes:
        raise AdmissionRefused("file_too_large")
    state.report.total_bytes += member.size
    if state.report.total_bytes > state.limits.max_total_bytes:
        raise AdmissionRefused("archive_too_large")
    source = archive.extractfile(member)
    data = source.read(member.size) if source is not None else b""
    if len(data) != member.size:
        raise AdmissionRefused("archive_malformed")
    state.findings.extend(scan_for_secrets(relative, data))
    write_new_file(
        state.destination,
        segments,
        data,
        executable=bool(member.mode & 0o100),
    )
    state.report.files += 1


def _unacknowledged(state: _Walk) -> tuple[SecretFinding, ...]:
    return tuple(
        f for f in state.findings if f"{f.path}:{f.rule}" not in state.allowed_findings
    )


def admit_archive(
    stream: BinaryIO,
    destination: Path,
    *,
    limits: AdmissionLimits | None = None,
    extra_deny_globs: tuple[str, ...] = (),
    allowed_findings: frozenset[str] = frozenset(),
) -> AdmissionReport:
    """Validate and extract ``stream`` into the empty ``destination`` directory."""
    deny_globs = tighten_deny_globs(extra_deny_globs)
    if any(destination.iterdir()):
        raise AdmissionRefused("workspace_not_empty")
    state = _Walk(
        destination=destination,
        limits=limits or AdmissionLimits(),
        extra_deny_globs=deny_globs,
        allowed_findings=allowed_findings,
    )
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                _admit_member(state, archive, member)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise AdmissionRefused("archive_malformed") from exc
    if state.entries == 0:
        raise AdmissionRefused("archive_empty")
    pending = _unacknowledged(state)
    if pending:
        raise AdmissionRefused("secrets_suspected", pending)
    logger.info(
        "Snapshot admitted: %d files, %d excluded",
        state.report.files,
        len(state.report.excluded),
    )
    return state.report
