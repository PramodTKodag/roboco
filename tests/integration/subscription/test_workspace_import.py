"""Real-Docker proof of snapshot import, file operations and patch export."""

from __future__ import annotations

import tarfile

import pytest
from roboco.runtime.subscription.contracts import ImportOptions, WorkerOperation

from tests.integration.subscription.docker_support import (
    docker_unavailable_reason,
    new_imported_session,
    new_session,
    new_session_for,
    tar_of,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        docker_unavailable_reason() is not None,
        reason=docker_unavailable_reason() or "docker available",
    ),
]

SYNTHETIC_KEY = "sk-ant-" + "Z" * 40
PROJECT = {
    "app.py": b"def add(a, b):\n    return a + b\n",
    "README.md": b"# demo\n",
    ".env": b"DATABASE_URL=synthetic\n",
    "keys/server.pem": b"synthetic-pem",
}


def _symlink(target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo("placeholder")
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info


async def test_import_populates_the_workspace_excludes_denied_files_and_baselines(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(worker_image, created_volumes, tar_of(PROJECT))
    listing = await session.run_command("ls -A /workspace; cat /workspace/app.py")
    assert listing.ok, listing.reason_code
    out = listing.result.stdout
    assert "app.py" in out
    assert "README.md" in out
    assert ".env" not in out
    assert "keys" not in out
    log = await session.run_command("git -C /workspace log --oneline | wc -l")
    assert log.result.stdout.strip() == "1"
    remotes = await session.run_command("git -C /workspace remote | wc -l")
    assert remotes.result.stdout.strip() == "0"


async def test_edit_through_file_operations_exports_a_reviewable_patch(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(worker_image, created_volumes, tar_of(PROJECT))
    written = await session.execute(
        session.job(
            WorkerOperation.WRITE_FILE,
            {"path": "app.py", "content": "def add(a, b):\n    return b + a\n"},
        )
    )
    assert written.ok, written.reason_code
    exported = await session.execute(session.job(WorkerOperation.EXPORT_PATCH, {}))
    assert exported.ok, exported.reason_code
    patch = exported.result.stdout
    assert "-    return a + b" in patch
    assert "+    return b + a" in patch
    assert ".env" not in patch


async def test_hostile_archives_are_refused_and_leave_no_volume(
    worker_image: str, created_volumes: list[str]
) -> None:
    hostile = {
        "symlink": tar_of({"ok.txt": b"x", "link": _symlink("/etc/passwd")}),
        "traversal": tar_of({"ok.txt": b"x", "../../escape.txt": b"x"}),
        "absolute": tar_of({"/etc/cron.d/x": b"x"}),
    }
    for name, archive in hostile.items():
        session = new_session_for(worker_image, created_volumes)
        ok, outcome, reason = await session.import_archive(archive)
        assert not ok, name
        assert outcome is not None
        assert reason in {"entry_type_refused", "path_invalid"}, (name, reason)
        assert not session.volume_exists(), name


async def test_suspected_secrets_block_import_until_each_finding_is_acknowledged(
    worker_image: str, created_volumes: list[str]
) -> None:
    archive = tar_of({"settings.txt": f"token={SYNTHETIC_KEY}".encode()})
    blocked = new_session_for(worker_image, created_volumes)
    ok, outcome, reason = await blocked.import_archive(archive)
    assert not ok
    assert reason == "secrets_suspected"
    assert outcome is not None
    assert [(f.path, f.rule) for f in outcome.findings] == [
        ("settings.txt", "anthropic_key")
    ]
    assert SYNTHETIC_KEY not in outcome.model_dump_json()
    assert not blocked.volume_exists()

    allowed = new_session_for(worker_image, created_volumes)
    ok, _, reason = await allowed.import_archive(
        archive, ImportOptions(allowed_findings=("settings.txt:anthropic_key",))
    )
    assert ok, reason


async def test_a_workspace_can_only_be_imported_once(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_imported_session(worker_image, created_volumes, tar_of(PROJECT))
    ok, _, reason = await session.import_archive(tar_of({"other.txt": b"x"}))
    assert not ok
    assert reason == "workspace_exists"
    still_there = await session.run_command("ls /workspace")
    assert "app.py" in still_there.result.stdout


async def test_empty_register_path_still_yields_a_usable_workspace(
    worker_image: str, created_volumes: list[str]
) -> None:
    session = await new_session(worker_image, created_volumes)
    result = await session.run_command("echo hi")
    assert result.ok
