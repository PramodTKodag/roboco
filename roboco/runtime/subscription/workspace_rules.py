"""Platform rules for what may enter, and be touched in, a worker workspace.

Pure and stdlib-only: it runs inside the credential-free worker and in the
owner-side snapshot builder. Deny rules are fixed here; callers can only add
globs, never remove one.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from roboco.runtime.subscription.contracts import MAX_PATH_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterable

DENIED_DIRECTORIES = frozenset(
    {
        ".git",
        ".ssh",
        ".aws",
        ".gnupg",
        ".kube",
        ".docker",
        ".claude",
        ".azure",
        ".gcloud",
    }
)
DENIED_FILE_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".pgpass",
        ".git-credentials",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
DENIED_FILE_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.dump",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.rdb",
    "terraform.tfstate",
    "terraform.tfstate.*",
    ".env.*",
)
ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template"})
MAX_SECRET_SCAN_BYTES = 1024 * 1024
# Nothing here decodes percent escapes; refusing the encodings of "." "/" and "\\"
# keeps an encoded traversal from surprising any later layer. Also refused: NUL,
# backslash, absolute or home-relative starts and drive-letter prefixes.
_UNSAFE_PATH_TEXT = re.compile(
    r"[\x00\\]|%(?:2e|2f|5c)|^[/~]|^[A-Za-z]:", re.IGNORECASE
)
_DOT_SEGMENTS = frozenset({"", ".", ".."})


class InvalidWorkspacePath(ValueError):
    """A path that could escape the workspace or confuse a later layer."""


def split_archive_path(raw: str) -> tuple[str, ...]:
    """Validate an archive/relative path and return its segments.

    Trailing slashes (directory entries) are tolerated by stripping exactly one;
    everything else that is not a plain relative, normalised path is refused.
    """
    path = raw[:-1] if raw.endswith("/") and len(raw) > 1 else raw
    too_long = len(path.encode("utf-8", errors="surrogateescape")) > MAX_PATH_BYTES
    if not path or too_long or _UNSAFE_PATH_TEXT.search(path):
        raise InvalidWorkspacePath("path_invalid")
    segments = tuple(path.split("/"))
    if _DOT_SEGMENTS.intersection(segments):
        raise InvalidWorkspacePath("path_invalid")
    return segments


def tighten_deny_globs(globs: Iterable[str]) -> tuple[str, ...]:
    """Accept owner-supplied extra deny globs; refuse anything that could weaken."""
    accepted: list[str] = []
    for glob in globs:
        if (
            not glob
            or glob.startswith(("!", "/", "~"))
            or "\x00" in glob
            or ".." in glob.split("/")
        ):
            raise ValueError("deny glob is malformed or attempts to weaken the rules")
        accepted.append(glob)
    return tuple(accepted)


def is_denied_path(path: str, extra_globs: tuple[str, ...] = ()) -> bool:
    segments = split_archive_path(path)
    if any(segment in DENIED_DIRECTORIES for segment in segments[:-1]):
        return True
    name = segments[-1]
    if name in DENIED_DIRECTORIES or name in DENIED_FILE_NAMES:
        return True
    if name in ENV_TEMPLATE_NAMES:
        return False
    if any(fnmatch.fnmatchcase(name, glob) for glob in DENIED_FILE_GLOBS):
        return True
    return any(fnmatch.fnmatchcase(path, glob) for glob in extra_globs)


@dataclass(frozen=True)
class SecretFinding:
    """A suspected secret. Carries the location and rule, never the matched text."""

    path: str
    rule: str


_SECRET_RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("anthropic_key", re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}")),
    (
        "private_key_block",
        re.compile(rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    ),
    ("aws_access_key_id", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "generic_secret_assignment",
        re.compile(
            rb"""(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*"""
            rb"""['"][^'"\s]{16,}['"]"""
        ),
    ),
)


def scan_for_secrets(path: str, content: bytes) -> list[SecretFinding]:
    """Heuristic scan of one file. A hit asks the owner to resolve it; a clean
    result is not proof of absence, which is why isolation never relies on it."""
    window = content[:MAX_SECRET_SCAN_BYTES]
    return [
        SecretFinding(path=path, rule=rule)
        for rule, pattern in _SECRET_RULES
        if pattern.search(window)
    ]
