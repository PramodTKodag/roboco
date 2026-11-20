"""No credential-shaped value reaches rendered logs, whatever field carries it.

Every sentinel below is synthetic. The checks run through the real structlog
configuration and the real request-logging middleware.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from roboco.api.middleware import RequestLoggingMiddleware
from roboco.logging import redact_event_dict, setup_logging

if TYPE_CHECKING:
    from collections.abc import Iterator

OAUTH_ACCESS = "sk-ant-oat01-SYNTHETICaccessTOKENvalue000000000000"
OAUTH_REFRESH = "sk-ant-ort01-SYNTHETICrefreshTOKENvalue00000000000"
API_KEY = "sk-ant-api03-SYNTHETICapiKEYvalue00000000000000000"
GENERIC_KEY = "sk-SYNTHETICgenericKEYvalue000000000000"
SESSION_COOKIE = "roboco_session=SYNTHETICsessionCOOKIEvalue"
DB_URL = "postgresql+asyncpg://roboco:SYNTHETICdbPASSWORD@db:5432/roboco"
REDIS_URL = "redis://:SYNTHETICredisPASSWORD@redis:6379/0"
RUN_TOKEN = "eyJydW4iOiJzeW50aGV0aWMifQ." + "a1" * 32
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGljc2ln"
SENTINELS = (
    "SYNTHETICaccessTOKEN",
    "SYNTHETICrefreshTOKEN",
    "SYNTHETICapiKEY",
    "SYNTHETICgenericKEY",
    "SYNTHETICsessionCOOKIE",
    "SYNTHETICdbPASSWORD",
    "SYNTHETICredisPASSWORD",
    "eyJydW4iOiJzeW50aGV0aWMifQ",
    "c3ludGhldGljc2ln",
)


def _redacted(event: dict[str, object]) -> str:
    return json.dumps(redact_event_dict(None, "info", dict(event)), default=str)


@pytest.mark.parametrize(
    "text",
    [
        OAUTH_ACCESS,
        OAUTH_REFRESH,
        API_KEY,
        GENERIC_KEY,
        f"Cookie: {SESSION_COOKIE}; theme=dark",
        f"Set-Cookie: {SESSION_COOKIE}; HttpOnly",
        f"connecting to {DB_URL}",
        f"broker {REDIS_URL}",
        f"header X: {RUN_TOKEN}",
        f"bearer {JWT}",
    ],
)
def test_credential_shapes_are_scrubbed_from_messages(text: str) -> None:
    rendered = _redacted({"event": text, "detail": text})
    assert not [s for s in SENTINELS if s in rendered]


def test_nested_and_listed_values_are_scrubbed() -> None:
    rendered = _redacted(
        {
            "event": "request",
            "query_params": {"next": "/", "note": OAUTH_ACCESS},
            "headers": [("cookie", SESSION_COOKIE), ("x", DB_URL)],
        }
    )
    assert not [s for s in SENTINELS if s in rendered]


@pytest.mark.parametrize(
    "key",
    ["token", "password", "api_key", "cookie", "authorization", "x-agent-token",
     "access_token", "client_secret", "db_password"],
)  # fmt: skip
def test_secret_named_fields_are_replaced_whatever_their_shape(key: str) -> None:
    rendered = _redacted({"event": "e", key: "plain-looking-value", "n": {key: "x"}})
    assert "plain-looking-value" not in rendered
    assert '"x"' not in rendered


def test_counters_named_like_tokens_are_kept() -> None:
    rendered = _redacted({"event": "usage", "token_count": 12, "input_tokens": 7})
    assert '"token_count": 12' in rendered
    assert '"input_tokens": 7' in rendered


@pytest.fixture
def request_log(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    # Capture must be live before logging is configured so its handlers bind to it.
    request.getfixturevalue("capfd")
    setup_logging()
    # The test client's own transport logs the full URL; only the server's logs
    # are under test.
    for client_logger in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(client_logger).setLevel(logging.WARNING)
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/probe")
    async def _probe() -> dict[str, str]:
        return {"ok": "yes"}

    yield TestClient(app)
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()


def test_request_logging_never_prints_credentials_from_the_query_string(
    request_log: TestClient, capfd: pytest.CaptureFixture[str]
) -> None:
    request_log.get(
        "/probe", params={"token": OAUTH_ACCESS, "key": API_KEY, "q": "hello"}
    )
    out = capfd.readouterr()
    combined = out.out + out.err
    assert "probe" in combined
    assert not [s for s in SENTINELS if s in combined]
