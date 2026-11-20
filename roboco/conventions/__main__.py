"""``python -m roboco.conventions check --root <dir> --files <a> <b> ...``.

Builds the effective map from the repo's ``.roboco/conventions.yml`` overlaid
on auto-derived defaults, runs the validator over the named files, and prints
one JSON finding per line. Exit 0 when it ran (findings may be empty or
present); exit 3 when it could not run (a broken config or a grammar failure),
with ``{"error": ...}`` on stderr — the fail-loud signal the gate blocks on.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .check import check_files

_EXIT_COULD_NOT_RUN = 3


def _fail(reason: str) -> int:
    print(json.dumps({"error": reason}), file=sys.stderr)
    return _EXIT_COULD_NOT_RUN


def _run_check(root: Path, files: list[str]) -> int:
    outcome = check_files(root, files)
    if outcome.could_not_run_reason is not None:
        return _fail(outcome.could_not_run_reason)
    for line in outcome.finding_lines:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse args and run the requested command. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m roboco.conventions")
    subcommands = parser.add_subparsers(dest="command", required=True)
    check = subcommands.add_parser("check", help="check changed files")
    check.add_argument("--root", required=True, type=Path)
    check.add_argument("--files", nargs="*", default=[])
    args = parser.parse_args(argv)
    return _run_check(args.root, list(args.files))


if __name__ == "__main__":
    raise SystemExit(main())
