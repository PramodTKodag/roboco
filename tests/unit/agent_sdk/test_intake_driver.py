"""extract_draft: turns a fenced ``roboco-draft`` reply block into a draft dict."""

from __future__ import annotations

from roboco.agent_sdk.intake_driver import extract_draft


def test_fenced_block_yields_the_parsed_draft() -> None:
    text = (
        "Here is the task.\n"
        "```roboco-draft\n"
        '{"title": "Add metrics", "acceptance_criteria": ["x"], "scale": "single"}\n'
        "```\n"
    )
    draft = extract_draft(text)
    assert draft is not None
    assert draft["title"] == "Add metrics"
    assert draft["scale"] == "single"


def test_reply_without_a_block_has_no_draft() -> None:
    assert extract_draft("just a chat reply") is None


def test_malformed_json_block_is_ignored() -> None:
    assert extract_draft("```roboco-draft\n{not valid json}\n```") is None


def test_block_without_a_title_is_not_a_draft() -> None:
    assert extract_draft('```roboco-draft\n{"acceptance_criteria": []}\n```') is None


def test_lone_scalar_spec_fields_are_wrapped_into_lists() -> None:
    # The panel maps over these fields; a bare string or object must never reach it.
    text = (
        "```roboco-draft\n"
        '{"title": "Single cell", "acceptance_criteria": "one string",'
        ' "the_work": {"team": "backend", "summary": "do it", "items": "lone"}}\n'
        "```"
    )
    draft = extract_draft(text)
    assert draft is not None
    assert draft["acceptance_criteria"] == ["one string"]
    assert isinstance(draft["the_work"], list) and len(draft["the_work"]) == 1
    unit = draft["the_work"][0]
    assert unit["team"] == "backend"
    assert unit["items"] == ["lone"]


def test_non_list_non_dict_work_is_dropped() -> None:
    draft = extract_draft(
        '```roboco-draft\n{"title": "Bad work", "the_work": "not a list"}\n```'
    )
    assert draft is not None
    assert draft["the_work"] == []
