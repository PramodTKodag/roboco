"""Public MCP names of flow verbs.

The lifecycle names a verb by its intent (``pass_review``); the MCP surface uses
Python-friendly public names (``pass``). The flow server registers tools under the
public name and the trusted model service must predict exactly the same names, so
the mapping lives here for both.
"""

from __future__ import annotations

from typing import Final

INTENT_TO_PUBLIC: Final[dict[str, str]] = {
    "pass_review": "pass",
    "fail_review": "fail",
}


def public_flow_verb(intent: str) -> str:
    """The MCP tool name a flow intent is registered under."""
    return INTENT_TO_PUBLIC.get(intent, intent)
