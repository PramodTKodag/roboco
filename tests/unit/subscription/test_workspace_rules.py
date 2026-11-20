"""Platform deny rules, archive path validation and secret scanning."""

from __future__ import annotations

import pytest
from roboco.runtime.subscription.workspace_rules import (
    InvalidWorkspacePath,
    is_denied_path,
    scan_for_secrets,
    split_archive_path,
    tighten_deny_globs,
)


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "app/.env",
        ".env.production",
        ".env.local",
        ".git/config",
        "sub/.git/hooks/pre-commit",
        ".ssh/id_rsa",
        "home/.aws/credentials",
        ".claude/.credentials.json",
        ".npmrc",
        "pkg/.pypirc",
        ".netrc",
        ".pgpass",
        ".git-credentials",
        "keys/server.pem",
        "keys/server.key",
        "id_ed25519",
        "dump.sqlite3",
        "backup/prod.dump",
        "infra/terraform.tfstate",
        "infra/terraform.tfstate.backup",
        "store.p12",
        "release.keystore",
        ".kube/config",
        ".docker/config.json",
    ],
)
def test_platform_deny_rules_match_credential_and_config_paths(path: str) -> None:
    assert is_denied_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "README.md",
        ".env.example",
        "config/.env.sample",
        ".env.template",
        "docs/environment.md",
        ".github/workflows/ci.yml",
        "pyproject.toml",
        "tests/fixtures/data.json",
    ],
)
def test_ordinary_paths_and_env_templates_are_allowed(path: str) -> None:
    assert is_denied_path(path) is False


def test_extra_globs_can_only_add_denials() -> None:
    extra = tighten_deny_globs(["secrets/*", "*.bak"])
    assert is_denied_path("secrets/a.txt", extra) is True
    assert is_denied_path("x.bak", extra) is True
    assert is_denied_path(".env", extra) is True
    assert is_denied_path("src/app.py", extra) is False


@pytest.mark.parametrize("glob", ["!.env", "", "/abs/*", "../x", "a\x00b"])
def test_weakening_or_malformed_globs_are_refused(glob: str) -> None:
    with pytest.raises(ValueError):
        tighten_deny_globs([glob])


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "../x",
        "a/../../x",
        "a//b",
        "a/./b",
        "",
        ".",
        "..",
        "a\x00b",
        "a\\b",
        "C:/x",
        "~root/x",
        "a/b//",
        "%2e%2e/x",
        "x" * 2000,
    ],
)
def test_archive_paths_that_could_escape_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidWorkspacePath):
        split_archive_path(raw)


def test_valid_archive_path_splits_into_segments() -> None:
    assert split_archive_path("src/pkg/mod.py") == ("src", "pkg", "mod.py")
    assert split_archive_path("dir/") == ("dir",)


def test_percent_encoded_traversal_is_not_decoded_but_flagged() -> None:
    # No component of the pipeline decodes percent escapes, so the literal text
    # is a harmless filename; reject anyway so it never surprises a later layer.
    with pytest.raises(InvalidWorkspacePath):
        split_archive_path("a/%2e%2e/b")


def test_secret_scan_reports_rule_names_never_values() -> None:
    secret = "sk-ant-" + "A" * 40
    findings = scan_for_secrets("cfg.txt", f"key = {secret}\n".encode())
    assert [f.rule for f in findings] == ["anthropic_key"]
    assert secret not in repr(findings)
    assert findings[0].path == "cfg.txt"


@pytest.mark.parametrize(
    ("content", "rule"),
    [
        (b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "private_key_block"),
        (b"AKIAABCDEFGHIJKLMNOP", "aws_access_key_id"),
        (b"ghp_" + b"a" * 36, "github_token"),
        (b"xoxb-123456789012-abcdefghijkl", "slack_token"),
        (b'password = "hunter2hunter2hunter2"', "generic_secret_assignment"),
    ],
)
def test_secret_scan_detects_common_credential_shapes(
    content: bytes, rule: str
) -> None:
    assert rule in {f.rule for f in scan_for_secrets("a.txt", content)}


def test_secret_scan_ignores_ordinary_text_and_binary_noise() -> None:
    assert scan_for_secrets("a.py", b"def f():\n    return 1\n") == []
    assert scan_for_secrets("a.bin", bytes(range(256)) * 4) == []
