"""Independent inspection of an exported patch before it becomes a review artifact.

The offline worker already filters denied paths when it exports a patch, but the
workspace is project-controlled, so the controller never trusts that filter. This
module re-reads the patch text on its own: every touched path must be a plain,
allowed, regular-file path, no link/submodule/mode changes or binary payloads are
accepted, and the added lines are scanned for suspected secrets. A refusal carries
a stable reason code and, for secrets, only rule names and paths, never values.
"""

from __future__ import annotations

from dataclasses import dataclass

from roboco.runtime.subscription.workspace_rules import (
    InvalidWorkspacePath,
    is_denied_path,
    scan_for_secrets,
    split_archive_path,
)

MAX_PATCH_FILES = 2000
_HEADER_PREFIX = "diff --git a/"
_HEADER_SEPARATOR = " b/"
_FORBIDDEN_MARKERS = (
    "GIT binary patch",
    "Binary files ",
    "old mode ",
    "new mode ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "similarity index ",
)
_LINK_OR_SUBMODULE_MODES = ("120000", "160000")
_DELETED_FILE_MARKER = "deleted file mode "
_MODE_LINES = ("new file mode ", _DELETED_FILE_MARKER, "index ")


class PatchRefused(Exception):
    """The patch cannot become an artifact. Carries a reason code, never content."""

    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(reason_code)


@dataclass(frozen=True)
class PatchReport:
    """What an accepted patch touches."""

    paths: tuple[str, ...]
    present_paths: tuple[str, ...]
    added_lines: int
    removed_lines: int


def _header_path(header: str) -> str:
    """The one path of ``diff --git a/P b/P``; renames and odd quoting are refused.

    The two sides must be the same path, so the path is recovered by length and
    spaces inside it need no guessing.
    """
    rest = header.removeprefix(_HEADER_PREFIX)
    half, remainder = divmod(len(rest) - len(_HEADER_SEPARATOR), 2)
    old, new = rest[:half], rest[half + len(_HEADER_SEPARATOR) :]
    if (
        remainder
        or half <= 0
        or old != new
        or rest[half : half + len(_HEADER_SEPARATOR)] != _HEADER_SEPARATOR
        or old.startswith('"')
    ):
        raise PatchRefused("patch_header_invalid")
    return old


def _section_path(header: str) -> str:
    path = _header_path(header)
    try:
        split_archive_path(path)
        if is_denied_path(path):
            raise PatchRefused("patch_path_denied", path)
    except InvalidWorkspacePath as exc:
        raise PatchRefused("patch_path_invalid") from exc
    return path


def _refuse_unsafe_line(line: str) -> None:
    if line.startswith(_FORBIDDEN_MARKERS):
        raise PatchRefused("patch_unsupported_change")
    if line.startswith(_MODE_LINES) and any(
        mode in line for mode in _LINK_OR_SUBMODULE_MODES
    ):
        raise PatchRefused("patch_link_or_submodule")


def _split_sections(patch: str) -> list[list[str]]:
    sections: list[list[str]] = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            sections.append([line])
        elif not sections:
            raise PatchRefused("patch_header_invalid")
        else:
            sections[-1].append(line)
    return sections


def _added_text(lines: list[str]) -> bytes:
    added = [
        line[1:]
        for line in lines
        if line.startswith("+") and not line.startswith("+++")
    ]
    return "\n".join(added).encode("utf-8", errors="replace")


_DOCUMENTATION_PREFIXES = ("docs/", "doc/")
_DOCUMENTATION_SUFFIXES = (".md", ".rst", ".txt")


def _is_documentation_path(path: str) -> bool:
    return path.startswith(_DOCUMENTATION_PREFIXES) or path.endswith(
        _DOCUMENTATION_SUFFIXES
    )


def _sections_by_path(patch: str) -> dict[str, str]:
    return {
        _section_path(lines[0]): "\n".join(lines) for lines in _split_sections(patch)
    }


def added_documentation_paths(reviewed: str, candidate: str) -> tuple[str, ...]:
    """Paths ``candidate`` adds to ``reviewed``, provided it only adds documentation.

    QA reviewed the exact bytes of ``reviewed``. A later documentation capture may
    add whole documentation files, but must leave every reviewed file section
    byte-for-byte unchanged; anything else is refused.
    """
    before = _sections_by_path(reviewed)
    after = _sections_by_path(candidate)
    for path, text in before.items():
        if after.get(path) != text:
            raise PatchRefused("documentation_altered_reviewed_change", path)
    added = tuple(path for path in after if path not in before)
    for path in added:
        if not _is_documentation_path(path):
            raise PatchRefused("documentation_adds_non_documentation", path)
    return added


def inspect_patch(patch: str) -> PatchReport:
    """Validate ``patch`` or raise ``PatchRefused``; an empty patch is refused."""
    if not patch.strip():
        raise PatchRefused("patch_empty")
    sections = _split_sections(patch)
    if len(sections) > MAX_PATCH_FILES:
        raise PatchRefused("patch_too_many_files")
    paths: list[str] = []
    present: list[str] = []
    added = removed = 0
    suspected: list[str] = []
    for lines in sections:
        path = _section_path(lines[0])
        if path in paths:
            raise PatchRefused("patch_duplicate_path", path)
        paths.append(path)
        if not _deletes_file(lines):
            present.append(path)
        for line in lines[1:]:
            _refuse_unsafe_line(line)
        added += _count_changed_lines(lines, "+", "+++")
        removed += _count_changed_lines(lines, "-", "---")
        suspected.extend(_secret_findings(path, lines))
    if suspected:
        raise PatchRefused("secrets_suspected", ",".join(sorted(suspected)))
    return PatchReport(
        paths=tuple(paths),
        present_paths=tuple(present),
        added_lines=added,
        removed_lines=removed,
    )


def _deletes_file(lines: list[str]) -> bool:
    return any(ln.startswith(_DELETED_FILE_MARKER) for ln in lines[1:])


def _count_changed_lines(lines: list[str], sign: str, header: str) -> int:
    return sum(1 for ln in lines if ln.startswith(sign) and not ln.startswith(header))


def _secret_findings(path: str, lines: list[str]) -> list[str]:
    return [
        f"{finding.path}:{finding.rule}"
        for finding in scan_for_secrets(path, _added_text(lines))
    ]
