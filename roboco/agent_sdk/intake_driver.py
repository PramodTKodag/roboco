"""Intake draft parsing — turns the intake agent's reply into a structured draft.

The intake agent emits the finished task draft as a fenced ``roboco-draft`` JSON
block. ``extract_draft`` mines it from the complete reply and coerces the
list-shaped spec fields so a malformed block never reaches the panel or a
``VARCHAR[]`` column.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# The intake agent emits the finished structured task draft as a fenced block
# (see the prompter system prompt) and is mined from the complete reply.
_DRAFT_FENCE = re.compile(r"```roboco-draft\s*\n(.*?)```", re.DOTALL)


def _coerce_to_list(value: Any) -> list[Any]:
    """Wrap a lone scalar/dict into a one-element list; drop junk to ``[]``.

    Mirrors ``content.validators.coerce_to_list`` but always returns a list
    (never ``None``) and never passes a non-list, non-scalar/dict through — the
    panel treats these fields as arrays and would crash on anything else. A
    bare string/dict is the well-intentioned single-item case (wrap it); a
    number/bool/None is not a work unit, so it is dropped.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str | dict):
        return [value]
    return []


# Fields that are lists of plain strings (vs ``the_work``, a list of work-unit
# dicts). The agent may emit these as XML-ish ``<item>…</item>`` elements, which
# the SDK parses into ``[{"item": {"$text": "…"}}, …]`` — coerce to flat str
# lists so they reach the panel and a VARCHAR[] column as strings, not dicts.
_STR_LIST_FIELDS = ("acceptance_criteria", "what_this_builds", "notes")


def _coerce_draft(data: Any) -> dict[str, Any] | None:
    """Return ``data`` as a draft dict (with a string ``title``), else ``None``.

    Accepts a dict, or a JSON string the agent may have passed. Coerces the
    list-shaped spec fields: ``the_work`` (a list of work-unit dicts) is wrapped
    to a list, and each ``the_work`` entry's ``items`` plus the string-list
    fields (``acceptance_criteria``, ``what_this_builds``, ``notes``) are
    flattened to ``list[str]`` — the agent sometimes emits these as XML-ish
    ``<item>…</item>`` elements that the SDK parses into dict wrappers, which
    would crash a ``VARCHAR[]`` insert and dump ``str(dict)`` into the rendered
    description. The panel renders ``(draft.the_work ?? []).map(...)`` and
    throws if ``the_work`` is a bare object, so this is the single choke point
    that keeps a non-array from ever reaching SSE.
    """
    from roboco.foundation.policy.content.validators import coerce_str_list

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    if not (isinstance(data, dict) and isinstance(data.get("title"), str)):
        return None
    coerced = dict(data)
    _coerce_spec_fields(coerced, coerce_str_list)
    return coerced


def _coerce_spec_fields(
    coerced: dict[str, Any], coerce: Callable[[Any], list[str]]
) -> None:
    """Coerce the list-shaped spec fields in place: wrap the_work, flatten the
    string-list fields, and flatten each the_work unit's items to ``list[str]``."""
    if "the_work" in coerced:
        coerced["the_work"] = _coerce_to_list(coerced["the_work"])
    for key in _STR_LIST_FIELDS:
        if key in coerced:
            coerced[key] = coerce(coerced[key])
    work = coerced.get("the_work")
    if isinstance(work, list):
        coerced["the_work"] = [
            {
                **unit,
                "items": coerce(unit.get("items")),
            }
            for unit in work
            if isinstance(unit, dict)
        ]


def extract_draft(text: str) -> dict[str, Any] | None:
    """Parse a fenced ``roboco-draft`` JSON block out of the agent's reply.

    A fallback to the ``propose_draft`` tool: returns the parsed object (a dict
    with a string ``title``) or ``None`` when no well-formed block is present.
    """
    match = _DRAFT_FENCE.search(text)
    if match is None:
        return None
    return _coerce_draft(match.group(1))
