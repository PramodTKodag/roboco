"""The service starts only on the approved CLI build."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from roboco.runtime.subscription import cli_pin, service_main
from roboco.runtime.subscription.cli_pin import CliBuildNotApproved, verify_cli_binary

_REPO_ROOT = Path(__file__).parents[3]
_SYNTHETIC_BINARY = b"synthetic cli build"


def _binary(tmp_path: Path, content: bytes = _SYNTHETIC_BINARY) -> Path:
    path = tmp_path / "claude"
    path.write_bytes(content)
    return path


def test_the_approved_digest_is_accepted_and_names_its_architecture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = hashlib.sha256(_SYNTHETIC_BINARY).hexdigest()
    monkeypatch.setattr(cli_pin, "PINNED_BINARY_SHA256", {"arm64": digest})
    assert verify_cli_binary(_binary(tmp_path)) == "arm64"


def test_any_other_build_is_refused_without_echoing_digests(tmp_path: Path) -> None:
    with pytest.raises(CliBuildNotApproved) as refusal:
        verify_cli_binary(_binary(tmp_path, b"a different build"))
    message = str(refusal.value)
    assert cli_pin.PINNED_CLI_VERSION in message
    assert hashlib.sha256(b"a different build").hexdigest() not in message


def test_a_missing_binary_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CliBuildNotApproved):
        verify_cli_binary(tmp_path / "absent")


def test_the_service_does_not_start_on_an_unapproved_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_path: Path) -> str:
        raise CliBuildNotApproved("not approved")

    def must_not_serve(*_args: object) -> None:
        raise AssertionError("the service started on an unapproved build")

    monkeypatch.setattr(service_main, "verify_cli_binary", refuse)
    monkeypatch.setattr(service_main.asyncio, "run", must_not_serve)
    assert service_main.main() == 2


def test_the_egress_policy_matches_its_reviewed_digest() -> None:
    directory = _REPO_ROOT / "deploy" / "personal"
    recorded = (directory / "egress-policy.sha256").read_text(encoding="utf-8").split()[0]
    policy = (directory / "egress-policy.json").read_bytes()
    assert hashlib.sha256(policy).hexdigest() == recorded
    assert json.loads(policy)["version"]
