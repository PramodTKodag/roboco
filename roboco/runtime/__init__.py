"""
Runtime Module for RoboCo

Manages Claude Code agent instances, lifecycle, and orchestration.

Names are exported lazily (PEP 562): importing a light subpackage such as
``roboco.runtime.subscription`` must not pull in the whole orchestrator, so the
small trusted components (credential-bearing service, job launcher) keep a
minimal import surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from roboco.runtime.orchestrator import (
        AgentInstance,
        AgentOrchestrator,
        AgentState,
    )
    from roboco.runtime.streaming import (
        ReasoningStreamCallback,
        get_reasoning_stream_callback,
        set_reasoning_stream_callback,
        stream_reasoning,
    )

_LAZY_EXPORTS: dict[str, str] = {
    "AgentInstance": "roboco.runtime.orchestrator",
    "AgentOrchestrator": "roboco.runtime.orchestrator",
    "AgentState": "roboco.runtime.orchestrator",
    "ReasoningStreamCallback": "roboco.runtime.streaming",
    "get_reasoning_stream_callback": "roboco.runtime.streaming",
    "set_reasoning_stream_callback": "roboco.runtime.streaming",
    "stream_reasoning": "roboco.runtime.streaming",
}

__all__ = [
    "AgentInstance",
    "AgentOrchestrator",
    "AgentState",
    "ReasoningStreamCallback",
    "get_reasoning_stream_callback",
    "set_reasoning_stream_callback",
    "stream_reasoning",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
