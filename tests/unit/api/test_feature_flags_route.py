"""The feature-flag listing marks policy-forbidden flags as locked."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from roboco.api.routes.settings import get_feature_flags
from roboco.foundation.policy.subscription_policy import FORBIDDEN_FEATURE_FLAGS
from roboco.services.settings import FEATURE_FLAGS


async def test_forbidden_flags_are_locked_and_off() -> None:
    effective = {key: False for key, _ in FEATURE_FLAGS}
    with patch(
        "roboco.api.routes.settings.feature_flag_effective_values",
        AsyncMock(return_value=effective),
    ):
        response = await get_feature_flags(db=AsyncMock())

    by_key = {flag.key: flag for flag in response.flags}
    locked = {key for key, flag in by_key.items() if flag.locked}
    assert locked == set(FORBIDDEN_FEATURE_FLAGS) & set(by_key)
    assert not any(by_key[key].enabled for key in locked)


async def test_every_forbidden_flag_that_is_panel_tunable_is_listed_locked() -> None:
    listed = {key for key, _ in FEATURE_FLAGS}
    effective = dict.fromkeys(listed, False)
    with patch(
        "roboco.api.routes.settings.feature_flag_effective_values",
        AsyncMock(return_value=effective),
    ):
        response = await get_feature_flags(db=AsyncMock())
    unlocked_forbidden = [
        flag.key
        for flag in response.flags
        if flag.key in FORBIDDEN_FEATURE_FLAGS and not flag.locked
    ]
    assert unlocked_forbidden == []
