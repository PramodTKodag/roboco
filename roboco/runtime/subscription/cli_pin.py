"""The one approved build of the official CLI, checked before the service starts.

The digests below are the contract in ``docs/personal/cli-contract.md``. Moving
to another CLI build means changing them here together with a passing
compatibility and security run; a binary that does not match never serves a run.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

PINNED_CLI_VERSION = "2.1.311"
PINNED_BINARY_SHA256: dict[str, str] = {
    "arm64": "2e2496c9a8065df93acb0fca2d341f59a6b8777649b2e025ab8c9bcd5eef61f3",
    "x64": "86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada",
}
_READ_CHUNK_BYTES = 1 << 20


class CliBuildNotApproved(Exception):
    """The CLI binary is missing or is not the approved build."""


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_cli_binary(path: Path) -> str:
    """Return the matched architecture, or refuse; never launches the binary."""
    try:
        actual = sha256_of_file(path)
    except OSError as exc:
        raise CliBuildNotApproved("the CLI binary is unreadable") from exc
    for arch, approved in PINNED_BINARY_SHA256.items():
        if actual == approved:
            logger.info(
                "CLI build %s (%s) matches the approved digest",
                PINNED_CLI_VERSION,
                arch,
            )
            return arch
    raise CliBuildNotApproved(
        f"the CLI binary does not match approved build {PINNED_CLI_VERSION}"
    )
