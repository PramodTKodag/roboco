"""The owner stack tool: credential preflight, generated state, login report."""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cryptography.fernet import Fernet

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPTS = Path(__file__).parents[3] / "scripts" / "personal"
_SYNTHETIC_KEY = "synthetic-credential-value-not-real"
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_READ_ONLY_TOKEN_MODE = 0o444
_MIN_TOKEN_LENGTH = 32


@pytest.fixture(scope="module")
def stack() -> ModuleType:
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            "personal_stack_tool", _SCRIPTS / "stack.py"
        )
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
    return module


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_inherited_provider_variables_are_refused_by_name_only(
    stack: ModuleType,
) -> None:
    environment = {"ANTHROPIC_API_KEY": _SYNTHETIC_KEY, "PATH": "/usr/bin"}
    with pytest.raises(stack.StackRefused) as refusal:
        stack.refuse_inherited_credentials(environment)
    assert "ANTHROPIC_API_KEY" in str(refusal.value)
    assert _SYNTHETIC_KEY not in str(refusal.value)


def test_a_clean_environment_is_accepted(stack: ModuleType) -> None:
    stack.refuse_inherited_credentials({"PATH": "/usr/bin", "HOME": "/home/owner"})


def test_state_is_private_complete_and_never_rotated(
    stack: ModuleType, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    stack.ensure_state(state)
    assert _mode(state) == _PRIVATE_DIRECTORY_MODE
    assert _mode(state / "secrets.env") == _PRIVATE_FILE_MODE
    assert _mode(state / "panel-login.txt") == _PRIVATE_FILE_MODE
    for name in stack.TOKEN_NAMES:
        token = state / "secrets" / name
        assert len(token.read_text(encoding="utf-8")) >= _MIN_TOKEN_LENGTH
        assert _mode(token) == _READ_ONLY_TOKEN_MODE
    values = stack._read_env_file(state / "secrets.env")
    assert set(values) == {
        "ROBOCO_DATABASE_PASSWORD",
        "ROBOCO_ENCRYPTION_KEY",
        "ROBOCO_AGENT_AUTH_SECRET",
        "ROBOCO_CLOUD_AUTH_SECRET",
        "ROBOCO_CLOUD_AUTH_EMAIL",
        "ROBOCO_CLOUD_AUTH_PASSWORD",
    }
    assert len(set(values.values())) == len(values)
    before = (state / "secrets.env").read_text(encoding="utf-8")
    stack.ensure_state(state)
    assert (state / "secrets.env").read_text(encoding="utf-8") == before


def test_the_encryption_key_is_a_valid_fernet_key(
    stack: ModuleType, tmp_path: Path
) -> None:
    stack.ensure_state(tmp_path / "state")
    key = stack._read_env_file(tmp_path / "state" / "secrets.env")[
        "ROBOCO_ENCRYPTION_KEY"
    ]
    Fernet(key.encode())


def test_an_unreviewed_egress_policy_blocks_startup(
    stack: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = tmp_path / "egress-policy.json"
    policy.write_text('{"version": 1}', encoding="utf-8")
    digest = tmp_path / "egress-policy.sha256"
    digest.write_text("0" * 64, encoding="utf-8")
    monkeypatch.setattr(stack, "POLICY_FILE", policy)
    monkeypatch.setattr(stack, "POLICY_DIGEST_FILE", digest)
    with pytest.raises(stack.StackRefused):
        stack.policy_digest()


def test_denied_targets_are_distinct_ordered_and_skip_allowed_lines(
    stack: ModuleType,
) -> None:
    log = "\n".join(
        [
            "egress deny connect CONNECT endpoint=- "
            "reason=host_not_allowed target=a.example:443",
            "egress allow request POST endpoint=x reason=allowed",
            "egress deny connect CONNECT endpoint=- "
            "reason=host_not_allowed target=a.example:443",
            "egress deny request GET endpoint=- "
            "reason=path_not_allowed target=b.example:443/oauth/token",
            "egress deny dns endpoint=- reason=unexpected_protocol",
        ]
    )
    assert stack.denied_targets(log) == ["a.example:443", "b.example:443/oauth/token"]


def test_import_refuses_a_home_directory_its_parents_and_files(
    stack: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "users" / "owner"
    project = home / "code" / "project"
    project.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    for refused in (home, home.parent, tmp_path):
        with pytest.raises(stack.StackRefused):
            stack.validate_import_source(refused)
    with pytest.raises(stack.StackRefused):
        stack.validate_import_source(project / "missing")
    assert stack.validate_import_source(project) == project.resolve()


@pytest.mark.parametrize("port", ["80", "abc", "70000", ""])
def test_the_published_port_must_be_an_unprivileged_port(
    stack: ModuleType, monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    monkeypatch.setenv("ROBOCO_PERSONAL_PORT", port)
    with pytest.raises(stack.StackRefused):
        stack.owner_url()


def test_the_default_port_is_used_when_unset(
    stack: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ROBOCO_PERSONAL_PORT", raising=False)
    assert stack.owner_url() == "http://localhost:3300"
