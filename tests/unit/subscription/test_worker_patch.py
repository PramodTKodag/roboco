"""Local git baseline, patch application and patch export (offline worker side)."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest
from roboco.runtime.subscription.worker_files import WorkspaceFileError
from roboco.runtime.subscription.worker_patch import (
    accept_changes,
    apply_patch,
    export_patch,
    init_baseline,
)

if TYPE_CHECKING:
    from pathlib import Path


def _reason(excinfo: pytest.ExceptionInfo[WorkspaceFileError]) -> str:
    return excinfo.value.reason_code


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    (root / "keep.py").write_text("x = 1\n")
    init_baseline(root)
    return root


def _diff(root: Path, name: str, new: str) -> str:
    path = root / name
    old = path.read_text() if path.exists() else None
    path.write_text(new)
    out = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--no-color",
            "/dev/null" if old is None else str(path) + ".orig",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out


def _patch(old: str, new: str, name: str = "a.txt") -> str:
    return (
        f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n"
        f"@@ -1,3 +1,3 @@\n{old}{new}"
    )


GOOD_PATCH = (
    "diff --git a/a.txt b/a.txt\n"
    "--- a/a.txt\n"
    "+++ b/a.txt\n"
    "@@ -1,3 +1,3 @@\n"
    " one\n"
    "-two\n"
    "+TWO\n"
    " three\n"
)


def test_baseline_has_a_single_commit_and_no_hooks_or_remotes(workspace: Path) -> None:
    log = subprocess.run(
        ["git", "-C", str(workspace), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(log) == 1
    assert (
        not list((workspace / ".git" / "hooks").glob("*"))
        if (workspace / ".git" / "hooks").exists()
        else True
    )
    remotes = subprocess.run(
        ["git", "-C", str(workspace), "remote"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert remotes.strip() == ""


def test_valid_patch_applies_and_shows_in_the_export(workspace: Path) -> None:
    apply_patch(workspace, GOOD_PATCH)
    assert (workspace / "a.txt").read_text() == "one\nTWO\nthree\n"
    exported = export_patch(workspace).text
    assert "+TWO" in exported
    assert "-two" in exported


def test_patch_that_does_not_apply_changes_nothing(workspace: Path) -> None:
    bad = GOOD_PATCH.replace(" one\n", " ONE\n")
    with pytest.raises(WorkspaceFileError) as caught:
        apply_patch(workspace, bad)
    assert _reason(caught) == "patch_rejected"
    assert (workspace / "a.txt").read_text() == "one\ntwo\nthree\n"


@pytest.mark.parametrize(
    "target", [".env", ".git/config", "k.pem", "../escape.txt", "/etc/passwd"]
)
def test_patch_touching_denied_or_escaping_paths_is_refused(
    workspace: Path, target: str
) -> None:
    patch = (
        f"diff --git a/{target} b/{target}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{target}\n"
        "@@ -0,0 +1 @@\n"
        "+SECRET\n"
    )
    with pytest.raises(WorkspaceFileError) as caught:
        apply_patch(workspace, patch)
    assert _reason(caught) in {"patch_denied", "patch_rejected"}
    assert not (workspace / ".env").exists()
    assert not (workspace.parent / "escape.txt").exists()


def test_patch_creating_a_symlink_is_refused(workspace: Path) -> None:
    patch = (
        "diff --git a/link b/link\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        "+++ b/link\n"
        "@@ -0,0 +1 @@\n"
        "+/etc/passwd\n"
        "\\ No newline at end of file\n"
    )
    with pytest.raises(WorkspaceFileError) as caught:
        apply_patch(workspace, patch)
    assert _reason(caught) == "patch_denied"
    assert not os.path.lexists(workspace / "link")


def test_patch_cannot_write_through_a_planted_symlink(
    workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "ln").symlink_to(outside)
    patch = (
        "diff --git a/ln/x.txt b/ln/x.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/ln/x.txt\n"
        "@@ -0,0 +1 @@\n"
        "+payload\n"
    )
    with pytest.raises(WorkspaceFileError):
        apply_patch(workspace, patch)
    assert list(outside.iterdir()) == []


def test_oversized_or_binary_patches_are_refused(workspace: Path) -> None:
    with pytest.raises(WorkspaceFileError) as binary:
        apply_patch(
            workspace,
            "diff --git a/b.bin b/b.bin\nnew file mode 100644\n"
            "index 0000000..1111111\nGIT binary patch\nliteral 1\nIc${\n\n",
        )
    assert _reason(binary) in {"patch_rejected", "patch_denied"}


def test_export_includes_new_files_and_skips_denied_ones(workspace: Path) -> None:
    (workspace / "new.py").write_text("print(1)\n")
    (workspace / ".env").write_text("SECRET=1\n")
    (workspace / "k.pem").write_text("PRIVATE")
    exported = export_patch(workspace).text
    assert "new.py" in exported
    assert "SECRET" not in exported
    assert "PRIVATE" not in exported
    assert ".env" not in exported


def test_export_of_an_unchanged_workspace_is_empty(workspace: Path) -> None:
    assert export_patch(workspace).text == ""


def test_export_ignores_project_git_hooks_and_config_injection(workspace: Path) -> None:
    marker = workspace.parent / "hook-ran"
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    config = workspace / ".git" / "config"
    config.write_text(config.read_text() + f"\n[core]\n\tfsmonitor = touch {marker}\n")
    (workspace / "n.txt").write_text("x\n")
    export_patch(workspace)
    apply_patch(workspace, GOOD_PATCH)
    assert not marker.exists()


def test_export_marks_truncation_when_the_diff_is_large(workspace: Path) -> None:
    (workspace / "big.txt").write_text("line\n" * 40000)
    out = export_patch(workspace)
    assert out.truncated is True


def _commit_count(root: Path) -> int:
    counted = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(counted.stdout)


def test_accepting_changes_makes_them_the_new_baseline(workspace: Path) -> None:
    apply_patch(workspace, GOOD_PATCH)
    assert export_patch(workspace).text != ""
    assert accept_changes(workspace).text == "accepted"
    assert export_patch(workspace).text == ""
    assert (workspace / "a.txt").read_text() == "one\nTWO\nthree\n"
    assert _commit_count(workspace) == 2  # noqa: PLR2004 - baseline plus accepted


def test_the_next_export_after_an_accept_holds_only_later_changes(
    workspace: Path,
) -> None:
    apply_patch(workspace, GOOD_PATCH)
    accept_changes(workspace)
    (workspace / "keep.py").write_text("x = 2\n")
    exported = export_patch(workspace).text
    assert "+x = 2" in exported
    assert "TWO" not in exported


def test_accepting_an_unchanged_workspace_still_succeeds(workspace: Path) -> None:
    assert accept_changes(workspace).text == "accepted"
    assert export_patch(workspace).text == ""


def test_accept_fails_with_a_reason_when_there_is_no_repository(
    tmp_path: Path,
) -> None:
    with pytest.raises(WorkspaceFileError) as excinfo:
        accept_changes(tmp_path)
    assert _reason(excinfo) == "accept_failed"
