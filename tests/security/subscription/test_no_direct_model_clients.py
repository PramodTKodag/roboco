"""No controller code opens a connection to a model server on its own.

Model work runs through the trusted subscription runtime only. The direct
clients (an OpenAI-compatible chat endpoint, an embedding server, a model-list
probe) stay in the code base but are locked behind one policy gate each. Two
checks hold that line:

* a source scan: every function that reads a model-server URL from settings
  must pass through the gate (or be a listed config-only site), so a new
  client cannot appear unguarded;
* behavior: every client entry point refuses before any connection is made,
  with the HTTP layer and raw sockets patched to fail the test if touched.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from roboco.config import settings
from roboco.eval import runner as eval_runner
from roboco.services import (
    memory_distiller,
    optimal,
    vault_intake_engine,
    video_engine,
    x_engine,
)
from roboco.services.base import FeatureUnavailableError
from roboco.services.optimal_brain import indexer_worker, shared_embedder
from roboco.services.optimal_brain.validator import ValidatorService

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

REPO_ROOT = Path(__file__).parents[3]
SOURCE_ROOT = REPO_ROOT / "roboco"

MODEL_SERVER_URL_SETTINGS = frozenset({"local_llm_base_url", "ollama_base_url"})
GATE_NAMES = frozenset(
    {
        "require_direct_model_clients",
        "require_semantic_kb",
        "semantic_kb_available",
        "direct_model_clients_enabled",
    }
)

# Functions that read a model-server URL without opening a connection, each
# with the reason it is safe. The scan fails if an entry outlives its function.
CONFIG_ONLY_SITES: dict[tuple[str, str], str] = {
    (
        "roboco/services/optimal_brain/indexes/base.py",
        "from_settings",
    ): "builds a config object; the connection is gated in the synthesis call",
    (
        "roboco/services/optimal_brain/ollama_embedder.py",
        "__init__",
    ): "stores the URL; every constructor call is behind require_semantic_kb",
}


def _referenced_url_setting(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in MODEL_SERVER_URL_SETTINGS
        and isinstance(node.value, ast.Name)
        and node.value.id == "settings"
    )


def _mentions_gate(function: ast.AST) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id in GATE_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in GATE_NAMES:
            return True
    return False


def _functions_reading_a_model_url() -> dict[tuple[str, str], bool]:
    """Map (module, function) -> whether the function mentions a policy gate."""
    found: dict[tuple[str, str], bool] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(_referenced_url_setting(child) for child in ast.walk(node)):
                found[(relative, node.name)] = _mentions_gate(node)
    return found


def test_every_model_server_reader_passes_through_a_policy_gate() -> None:
    ungated = sorted(
        site
        for site, gated in _functions_reading_a_model_url().items()
        if not gated and site not in CONFIG_ONLY_SITES
    )
    assert not ungated, (
        "function(s) read a model-server URL without the subscription policy "
        f"gate; call require_direct_model_clients() first: {ungated}"
    )


def test_config_only_allowlist_has_no_stale_entries() -> None:
    readers = _functions_reading_a_model_url()
    stale = sorted(set(CONFIG_ONLY_SITES) - set(readers))
    assert not stale, f"allowlisted functions no longer read a URL, remove: {stale}"


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test the moment any HTTP client or raw socket is opened."""

    def _forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a connection was attempted while the policy lock is on")

    monkeypatch.setattr(httpx, "AsyncClient", _forbidden)
    monkeypatch.setattr(httpx, "Client", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden)


@pytest.fixture(autouse=True)
def _locks_engaged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "direct_model_clients_enabled", False)
    monkeypatch.setattr(settings, "semantic_kb_enabled", False)


CHAT_ENTRY_POINTS: list[tuple[str, Callable[[str], Awaitable[Any]]]] = [
    ("eval judge", eval_runner._judge_chat),
    ("video engine", video_engine._chat),
    ("vault intake", vault_intake_engine._chat),
    ("x engine", x_engine._chat),
    ("memory distiller", memory_distiller._chat),
]


@pytest.mark.parametrize(
    "chat",
    [entry for _, entry in CHAT_ENTRY_POINTS],
    ids=[n for n, _ in CHAT_ENTRY_POINTS],
)
async def test_chat_clients_refuse_before_connecting(
    no_network: None, chat: Callable[[str], Awaitable[Any]]
) -> None:
    with pytest.raises(FeatureUnavailableError) as refused:
        await chat("synthetic prompt")
    assert refused.value.details["code"] == "feature_unavailable"


async def test_embedder_is_not_created(no_network: None) -> None:
    with pytest.raises(FeatureUnavailableError):
        await shared_embedder.get_shared_embedder()


async def test_semantic_kb_service_is_not_started(no_network: None) -> None:
    with pytest.raises(FeatureUnavailableError):
        await optimal.get_optimal_service()


def test_indexer_worker_embedder_is_not_created(no_network: None) -> None:
    with pytest.raises(FeatureUnavailableError):
        indexer_worker._build_indexer_embedder()


async def test_validator_marks_llm_unavailable_without_connecting(
    no_network: None,
) -> None:
    validator = ValidatorService.__new__(ValidatorService)
    await validator._test_llm_connectivity()
    assert validator._llm_available is False
