"""Controller-side inspection of an exported patch."""

from __future__ import annotations

import pytest
from roboco.runtime.subscription.patch_inspection import (
    PatchRefused,
    added_documentation_paths,
    inspect_patch,
)


def _new_file(path: str, *lines: str) -> str:
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


def _deleted_file(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        "index 1111111..0000000\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-old\n"
    )


def _reason(patch: str) -> str:
    with pytest.raises(PatchRefused) as raised:
        inspect_patch(patch)
    return raised.value.reason_code


def test_a_plain_patch_reports_paths_and_counts() -> None:
    report = inspect_patch(_new_file("app/health.py", "def healthz(): ...", "x = 1"))
    assert report.paths == ("app/health.py",)
    assert report.present_paths == ("app/health.py",)
    assert (report.added_lines, report.removed_lines) == (2, 0)


def test_a_deleted_file_is_not_present_after_the_patch() -> None:
    patch = _new_file("app/a.py", "a = 1") + _deleted_file("app/b.py")
    report = inspect_patch(patch)
    assert report.paths == ("app/a.py", "app/b.py")
    assert report.present_paths == ("app/a.py",)


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ("", "patch_empty"),
        ("   \n", "patch_empty"),
        ("not a diff\n", "patch_header_invalid"),
        (_new_file("config/.env", "A=1"), "patch_path_denied"),
        (_new_file("../escape.py", "a = 1"), "patch_path_invalid"),
        (_new_file("/abs.py", "a = 1"), "patch_path_invalid"),
        (_new_file("app/a.py", "a = 1") * 2, "patch_duplicate_path"),
    ],
)
def test_unsafe_or_malformed_patches_are_refused(patch: str, reason: str) -> None:
    assert _reason(patch) == reason


@pytest.mark.parametrize(
    ("marker", "reason"),
    [
        ("GIT binary patch", "patch_unsupported_change"),
        ("rename from app/a.py", "patch_unsupported_change"),
        ("old mode 100644", "patch_unsupported_change"),
        ("new file mode 120000", "patch_link_or_submodule"),
        ("new file mode 160000", "patch_link_or_submodule"),
    ],
)
def test_binary_rename_mode_link_and_submodule_changes_are_refused(
    marker: str, reason: str
) -> None:
    patch = _new_file("app/a.py", "a = 1").replace("new file mode 100644", marker, 1)
    assert _reason(patch) == reason


def test_suspected_secrets_are_refused_without_echoing_the_value() -> None:
    patch = _new_file("app/keys.py", "-----BEGIN PRIVATE KEY-----")
    with pytest.raises(PatchRefused) as raised:
        inspect_patch(patch)
    assert raised.value.reason_code == "secrets_suspected"
    assert "BEGIN PRIVATE KEY" not in raised.value.detail
    assert "app/keys.py" in raised.value.detail


def test_documentation_may_be_added_when_reviewed_sections_are_unchanged() -> None:
    reviewed = _new_file("app/health.py", "def healthz(): ...")
    candidate = reviewed + _new_file("docs/health.md", "# Health")
    assert added_documentation_paths(reviewed, candidate) == ("docs/health.md",)


def test_documentation_cannot_alter_a_reviewed_section() -> None:
    reviewed = _new_file("app/health.py", "def healthz(): ...")
    candidate = _new_file("app/health.py", "def healthz(): tampered")
    with pytest.raises(PatchRefused) as raised:
        added_documentation_paths(reviewed, candidate)
    assert raised.value.reason_code == "documentation_altered_reviewed_change"


def test_documentation_cannot_drop_a_reviewed_file() -> None:
    reviewed = _new_file("app/health.py", "def healthz(): ...")
    candidate = _new_file("docs/health.md", "# Health")
    with pytest.raises(PatchRefused) as raised:
        added_documentation_paths(reviewed, candidate)
    assert raised.value.reason_code == "documentation_altered_reviewed_change"


def test_documentation_cannot_add_source_files() -> None:
    reviewed = _new_file("app/health.py", "def healthz(): ...")
    candidate = reviewed + _new_file("app/extra.py", "x = 1")
    with pytest.raises(PatchRefused) as raised:
        added_documentation_paths(reviewed, candidate)
    assert raised.value.reason_code == "documentation_adds_non_documentation"
