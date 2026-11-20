"""Egress policy: telemetry on a shared host, spoofed identities and rebinding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from roboco.runtime.subscription.egress_policy import (
    PROFILE_MAINTENANCE,
    PROFILE_NORMAL,
    EgressPolicy,
    EgressPolicyInvalid,
    EgressRequest,
    NoPermittedAddress,
    decide_connect,
    decide_request,
    decide_response,
    load_policy,
    pin_address,
    policy_sha256,
)

DEPLOY = Path(__file__).resolve().parents[3] / "deploy" / "personal"
HOST = "api.anthropic.com"
SYNTHETIC_ID = "abc_123-XYZ"


def _document(**overrides: object) -> dict[str, object]:
    endpoint: dict[str, object] = {
        "id": "inference_messages",
        "host": HOST,
        "port": 443,
        "methods": ["POST"],
        "path": "/v1/messages",
        "query_keys": ["beta"],
        "content_types": ["application/json"],
    }
    endpoint.update(overrides)
    return {"version": 1, "profiles": {"normal": [endpoint], "maintenance": []}}


def _load(document: object) -> EgressPolicy:
    raw = json.dumps(document).encode()
    return load_policy(raw, expected_sha256=policy_sha256(raw))


@pytest.fixture(scope="module")
def policy() -> EgressPolicy:
    raw = (DEPLOY / "egress-policy.json").read_bytes()
    expected = (DEPLOY / "egress-policy.sha256").read_text().strip()
    return load_policy(raw, expected_sha256=expected)


def _request(**overrides: object) -> EgressRequest:
    base: dict[str, object] = {
        "method": "POST",
        "scheme": "https",
        "connect_host": HOST,
        "connect_port": 443,
        "sni": HOST,
        "host_header": HOST,
        "target": "/v1/messages?beta=true",
        "upgrade": False,
        "content_type": "application/json",
    }
    base.update(overrides)
    return EgressRequest(**base)  # type: ignore[arg-type]


def _reason(policy: EgressPolicy, **overrides: object) -> str:
    return decide_request(policy, PROFILE_NORMAL, _request(**overrides)).reason_code


def test_the_shipped_policy_loads_and_allows_only_inference(
    policy: EgressPolicy,
) -> None:
    decision = decide_request(policy, PROFILE_NORMAL, _request())
    assert decision.allowed
    assert decision.endpoint_id == "inference_messages"
    assert policy.endpoints(PROFILE_MAINTENANCE) == ()


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("POST", "/api/event_logging/v2/batch"),
        ("POST", "/api/eval/sdk-key"),
        ("GET", "/api/claude_cli/bootstrap"),
        ("GET", "/api/claude_code_penguin_mode"),
        ("GET", "/api/claude_code/organizations/metrics_enabled"),
        ("GET", "/api/claude_code/policy_limits"),
        ("GET", "/api/claude_code/settings"),
    ],
)
def test_telemetry_and_config_calls_on_the_inference_host_are_denied(
    policy: EgressPolicy, method: str, target: str
) -> None:
    assert not decide_request(
        policy, PROFILE_NORMAL, _request(method=method, target=target)
    ).allowed


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"target": "/v1/messages/batches"}, "path_not_allowed"),
        ({"target": "/v1/messages/count_tokens"}, "path_not_allowed"),
        ({"target": "/v1/messages/"}, "path_not_allowed"),
        ({"method": "GET"}, "method_not_allowed"),
        ({"method": "DELETE"}, "method_not_allowed"),
        ({"target": "/v1/messages?beta=true&debug=1"}, "query_not_allowed"),
        ({"target": "/v1/messages?beta=true&beta=false"}, "path_not_canonical"),
        ({"target": "/v1/messages?beta=%0d%0a"}, "query_not_allowed"),
        ({"content_type": "text/plain"}, "content_type_not_allowed"),
        ({"content_type": None}, "content_type_not_allowed"),
        ({"scheme": "http"}, "scheme_not_https"),
        ({"upgrade": True}, "upgrade_not_allowed"),
        ({"connect_port": 8443, "host_header": f"{HOST}:8443"}, "port_not_allowed"),
    ],
)
def test_requests_off_the_approved_shape_are_denied(
    policy: EgressPolicy, overrides: dict[str, object], reason: str
) -> None:
    assert _reason(policy, **overrides) == reason


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        f"{HOST}.evil.example",
        f"evil-{HOST}",
        f"x.{HOST}",
        "anthropic.com",
        "claude.ai",
    ],
)
def test_other_hosts_and_misleading_suffixes_are_denied(
    policy: EgressPolicy, host: str
) -> None:
    assert (
        _reason(policy, connect_host=host, sni=host, host_header=host)
        == "host_not_allowed"
    )


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "169.254.169.254", "10.0.0.5", "[::1]", "::1", "2130706433", "0x7f.1"],
)
def test_ip_literals_and_numeric_shorthands_are_denied(
    policy: EgressPolicy, host: str
) -> None:
    assert (
        _reason(policy, connect_host=host, sni=host, host_header=host)
        == "host_not_canonical"
    )


@pytest.mark.parametrize(
    "host", [f"{HOST}.", "API.ANTHROPIC.COM", f"user@{HOST}", f"{HOST}@evil.example"]
)
def test_non_canonical_hosts_are_denied(policy: EgressPolicy, host: str) -> None:
    assert _reason(policy, connect_host=host) == "host_not_canonical"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sni": "evil.example"},
        {"host_header": "evil.example"},
        {"host_header": f"{HOST}:444"},
        {"sni": f"{HOST}.evil.example"},
    ],
)
def test_sni_host_header_and_connect_target_must_agree(
    policy: EgressPolicy, overrides: dict[str, object]
) -> None:
    assert _reason(policy, **overrides) == "authority_mismatch"


@pytest.mark.parametrize(
    "host_header", [f"user@{HOST}", f"{HOST}:abc", f"{HOST}:00443", "[::1]:443", ""]
)
def test_malformed_authorities_are_denied(
    policy: EgressPolicy, host_header: str
) -> None:
    assert _reason(policy, host_header=host_header) == "authority_invalid"


@pytest.mark.parametrize(
    "target",
    [
        "http://evil.example/v1/messages",
        "//evil.example/v1/messages",
        "/v1/../v1/messages",
        "/v1/./messages",
        "/v1//messages",
        "/v1%2fmessages",
        "/v1/%2e%2e/messages",
        "/v1\\messages",
        "/v1/messages#frag",
        "/v1/messages\r\nHost: evil",
        "v1/messages",
    ],
)
def test_path_tricks_are_denied(policy: EgressPolicy, target: str) -> None:
    assert _reason(policy, target=target) == "path_not_canonical"


def test_connect_is_allowed_only_for_an_approved_name_on_the_tls_port(
    policy: EgressPolicy,
) -> None:
    assert decide_connect(policy, PROFILE_NORMAL, HOST, 443).allowed
    assert decide_connect(policy, PROFILE_NORMAL, HOST, 80).reason_code == (
        "port_not_allowed"
    )
    assert decide_connect(policy, PROFILE_NORMAL, "evil.example", 443).reason_code == (
        "host_not_allowed"
    )
    assert decide_connect(policy, PROFILE_NORMAL, "10.0.0.1", 443).reason_code == (
        "host_not_canonical"
    )
    assert decide_connect(policy, PROFILE_MAINTENANCE, HOST, 443).reason_code == (
        "host_not_allowed"
    )


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_never_followed(status: int) -> None:
    assert decide_response(status, has_location=True).reason_code == "redirect_blocked"
    assert decide_response(200, has_location=True).reason_code == "redirect_blocked"
    assert decide_response(200, has_location=False).allowed


@pytest.mark.parametrize(
    "resolved",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["172.16.0.9"],
        ["192.168.1.1"],
        ["169.254.169.254"],
        ["100.64.0.1"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["::1"],
        ["fe80::1"],
        ["::ffff:10.0.0.1"],
        ["2606:4700::1111"],
        ["203.0.113.5", "10.0.0.1"],
        ["not-an-address"],
        [],
    ],
)
def test_rebinding_and_non_public_answers_are_refused(resolved: list[str]) -> None:
    with pytest.raises(NoPermittedAddress):
        pin_address(resolved)


def test_a_public_ipv4_answer_is_pinned() -> None:
    assert pin_address(["160.79.104.10"]) == "160.79.104.10"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"", "policy_empty_or_oversized"),
        (b"   \n", "policy_empty_or_oversized"),
        (b"x" * 70_000, "policy_empty_or_oversized"),
    ],
)
def test_empty_or_oversized_policy_files_block_startup(
    raw: bytes, reason: str
) -> None:
    with pytest.raises(EgressPolicyInvalid) as raised:
        load_policy(raw, expected_sha256=policy_sha256(raw))
    assert raised.value.reason_code == reason


def test_a_policy_that_is_not_the_reviewed_one_blocks_startup() -> None:
    raw = json.dumps(_document()).encode()
    for expected in ("", "0" * 64):
        with pytest.raises(EgressPolicyInvalid) as raised:
            load_policy(raw, expected_sha256=expected)
        assert raised.value.reason_code == "policy_digest_mismatch"


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "*.anthropic.com"},
        {"host": ".anthropic.com"},
        {"host": "anthropic"},
        {"host": "127.0.0.1"},
        {"host": "API.anthropic.com"},
        {"port": 80},
        {"methods": []},
        {"methods": ["CONNECT"]},
        {"methods": ["POST", "POST"]},
        {"path": "/v1/messages/"},
        {"path": "v1/messages"},
        {"path": "/v1/../messages"},
        {"path": "/v1/*"},
        {"query_keys": ["Beta"]},
        {"id": "Bad Id"},
        {"unexpected": "field"},
    ],
)
def test_loose_or_malformed_endpoints_block_startup(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(EgressPolicyInvalid):
        _load(_document(**overrides))


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"version": 2, "profiles": {}},
        {"version": 1},
        {"version": 1, "profiles": {"normal": []}},
        {"version": 1, "profiles": {"normal": [], "maintenance": []}},
        {"version": 1, "profiles": {"normal": [], "maintenance": [], "extra": []}},
    ],
)
def test_malformed_policy_documents_block_startup(document: object) -> None:
    with pytest.raises(EgressPolicyInvalid):
        _load(document)


def test_dynamic_path_segments_accept_only_validated_ids() -> None:
    policy = _load(_document(path="/v1/items/{id}/status", methods=["GET"]))
    good = _request(
        method="GET", target=f"/v1/items/{SYNTHETIC_ID}/status", content_type=None
    )
    assert decide_request(policy, PROFILE_NORMAL, good).allowed
    for target in (
        "/v1/items/status",
        "/v1/items/a b/status",
        f"/v1/items/{SYNTHETIC_ID}/status/extra",
        f"/v1/items/{SYNTHETIC_ID}/other",
        "/v1/items/%61/status",
    ):
        assert not decide_request(
            policy, PROFILE_NORMAL, _request(method="GET", target=target)
        ).allowed


def test_the_committed_digest_matches_the_committed_policy() -> None:
    raw = (DEPLOY / "egress-policy.json").read_bytes()
    assert policy_sha256(raw) == (DEPLOY / "egress-policy.sha256").read_text().strip()
