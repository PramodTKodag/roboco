"""Import a local project directory into a new credential-free worker workspace.

Owner-operated. The directory is snapshotted without following links, fixed
credential/config paths are left out, and the archive is admitted inside the
offline worker. Suspected secrets stop the import until each exact finding is
acknowledged with ``--allow PATH:RULE``. Usage:

    python scripts/personal/import_workspace.py --source DIR --project-id UUID \
        [--exclude GLOB]... [--allow PATH:RULE]... [--dry-run]

The launcher admin token is read from the file named by
ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE; it is never accepted on the command line.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from roboco.runtime.subscription.contracts import ImportOptions, ImportOutcome
from roboco.runtime.subscription.launcher_client import LauncherAdmin
from roboco.runtime.subscription.launcher_main import DEFAULT_HOST, DEFAULT_PORT
from roboco.runtime.workspace_snapshot import (
    SnapshotError,
    SnapshotResult,
    build_snapshot,
)

logger = logging.getLogger("import_workspace")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--workspace-handle", type=UUID, default=None)
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    parser.add_argument("--allow", action="append", default=[], metavar="PATH:RULE")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def _admin_token() -> str:
    path = os.environ.get("ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE", "").strip()
    if not path:
        raise SystemExit("ROBOCO_LAUNCHER_ADMIN_TOKEN_FILE is required")
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise SystemExit("admin token file is unreadable") from None


def _print_snapshot(snapshot: SnapshotResult) -> None:
    print(f"snapshot: {snapshot.files} files, {snapshot.total_bytes} bytes")
    for relative, reason in snapshot.omitted:
        print(f"  omitted {relative}: {reason}")


def _print_outcome(outcome: ImportOutcome) -> None:
    if outcome.ok:
        print(f"imported: {outcome.files} files, {outcome.directories} directories")
        for relative in outcome.excluded:
            print(f"  excluded by worker {relative}")
        return
    print(f"refused: {outcome.reason_code}")
    for finding in outcome.findings:
        print(
            f"  suspected secret at {finding.path} ({finding.rule}); "
            f"re-run with --allow {finding.path}:{finding.rule} to accept"
        )


async def _import(args: argparse.Namespace, snapshot: SnapshotResult) -> int:
    handle = args.workspace_handle or uuid4()
    options = ImportOptions(
        extra_deny_globs=tuple(args.exclude), allowed_findings=tuple(args.allow)
    )
    admin = LauncherAdmin(args.host, args.port, _admin_token())
    outcome = await admin.import_workspace(
        handle, args.project_id, snapshot.archive, options
    )
    _print_outcome(outcome)
    if not outcome.ok:
        return EXIT_REFUSED
    print(f"workspace handle: {handle}")
    return EXIT_OK


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        snapshot = build_snapshot(args.source, extra_exclude_globs=tuple(args.exclude))
    except SnapshotError as error:
        print(f"snapshot failed: {error}", file=sys.stderr)
        return EXIT_USAGE
    _print_snapshot(snapshot)
    if args.dry_run:
        return EXIT_OK
    return asyncio.run(_import(args, snapshot))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
