"""Local git baseline, patch application and patch export inside the worker.

The repository is created here, from the admitted snapshot, with no original
history, remotes or credential helpers. Git is run with a clean environment and
with the repository-controlled settings that can execute programs (hooks,
filesystem monitor, pager, external diff) overridden on the command line.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 - fixed argv, no shell
from typing import TYPE_CHECKING

from roboco.runtime.subscription.contracts import MAX_STREAM_BYTES
from roboco.runtime.subscription.worker_files import FileOutput, WorkspaceFileError
from roboco.runtime.subscription.workspace_rules import (
    InvalidWorkspacePath,
    is_denied_path,
    split_archive_path,
)

if TYPE_CHECKING:
    from pathlib import Path

GIT_TIMEOUT_SECONDS = 60
MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
_BASELINE_MESSAGE = "workspace baseline"
_WORKER_HOME = "/tmp"  # nosec B108 - worker container tmpfs, not a host path
_GIT_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": _WORKER_HOME,
    "LANG": "C.UTF-8",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}
_GIT_BASE = (
    "git",
    "--no-optional-locks",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.pager=cat",
    "-c",
    "core.askPass=",
    "-c",
    "protocol.allow=never",
)
_UNSAFE_SUMMARY_MODES = ("mode 120000", "mode 160000")
_NUMSTAT_PATHLESS = re.compile(r"^\S+\t\S+\t$")


def _git(
    root: Path, *args: str, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # nosec B603 - fixed argv, no shell
            [*_GIT_BASE, *args],
            cwd=root,
            env=_GIT_ENV,
            input=stdin,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceFileError("git_timeout") from exc


def init_baseline(root: Path) -> None:
    """Create the local repository and commit the admitted snapshot as baseline."""
    steps = (
        ("-c", "init.templateDir=", "init", "--quiet"),
        ("add", "--all"),
        (
            "-c",
            "user.name=roboco-worker",
            "-c",
            "user.email=worker@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "--no-verify",
            "-m",
            _BASELINE_MESSAGE,
        ),
    )
    for step in steps:
        if _git(root, *step).returncode != 0:
            raise WorkspaceFileError("baseline_failed")


def _numstat_paths(raw: bytes) -> list[str]:
    tokens = raw.decode("utf-8", errors="replace").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token:
            index += 1
            continue
        if _NUMSTAT_PATHLESS.match(token):  # rename/copy: old and new follow
            paths.extend(tokens[index + 1 : index + 3])
            index += 3
            continue
        paths.append(token.split("\t", 2)[-1])
        index += 1
    return paths


def _check_patch_paths(paths: list[str]) -> None:
    for path in paths:
        try:
            split_archive_path(path)
        except InvalidWorkspacePath as exc:
            raise WorkspaceFileError("patch_denied") from exc
        if is_denied_path(path):
            raise WorkspaceFileError("patch_denied")


def apply_patch(root: Path, patch: str) -> FileOutput:
    """Apply one text patch atomically, refusing denied paths, links and binaries."""
    data = patch.encode("utf-8")
    if b"GIT binary patch" in data or b"\nBinary files " in data:
        raise WorkspaceFileError("patch_rejected")
    numstat = _git(root, "apply", "--numstat", "-z", stdin=data)
    if numstat.returncode != 0:
        raise WorkspaceFileError("patch_rejected")
    _check_patch_paths(_numstat_paths(numstat.stdout))
    summary = _git(root, "apply", "--summary", stdin=data)
    if summary.returncode != 0:
        raise WorkspaceFileError("patch_rejected")
    if any(m.encode() in summary.stdout for m in _UNSAFE_SUMMARY_MODES):
        raise WorkspaceFileError("patch_denied")
    applied = _git(root, "apply", "--whitespace=nowarn", stdin=data)
    if applied.returncode != 0:
        raise WorkspaceFileError("patch_rejected")
    return FileOutput("applied")


def _split_sections(diff: str) -> list[str]:
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    return [part for part in parts if part]


def export_patch(root: Path) -> FileOutput:
    """Diff of the workspace against its baseline, minus every denied path."""
    if _git(root, "add", "--intent-to-add", "--all").returncode != 0:
        raise WorkspaceFileError("export_failed")
    flags = ("--no-renames", "--no-ext-diff", "--no-textconv", "--no-color")
    names = _git(root, "diff", "--name-only", "-z", *flags, "HEAD")
    diff = _git(root, "diff", *flags, "HEAD")
    if names.returncode != 0 or diff.returncode != 0:
        raise WorkspaceFileError("export_failed")
    paths = [p for p in names.stdout.decode("utf-8", errors="replace").split("\0") if p]
    sections = _split_sections(
        diff.stdout[:MAX_GIT_OUTPUT_BYTES].decode("utf-8", errors="replace")
    )
    if len(diff.stdout) <= MAX_GIT_OUTPUT_BYTES and len(paths) != len(sections):
        raise WorkspaceFileError("export_failed")
    kept = [s for p, s in zip(paths, sections, strict=False) if not _is_denied(p)]
    return _bounded("".join(kept))


def _is_denied(path: str) -> bool:
    try:
        return is_denied_path(path)
    except InvalidWorkspacePath:
        return True


def _bounded(text: str) -> FileOutput:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_STREAM_BYTES:
        return FileOutput(text)
    return FileOutput(encoded[:MAX_STREAM_BYTES].decode("utf-8", errors="ignore"), True)
