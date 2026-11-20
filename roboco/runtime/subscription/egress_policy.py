"""Egress policy for the model runtime: what the TLS proxy may forward.

Pure rules, stdlib only, no IO, so the same file runs inside the proxy container
and under the unit tests. The policy is an explicit, versioned list of endpoints
(host, port, method, path shape, query keys); anything not listed is refused.
There is no wildcard host, no suffix match and no generic tunnel. A request is
checked against every identity it carries (CONNECT target, TLS server name, Host
header) and against the addresses the name resolved to, because validating one
and dialing another is how rebinding attacks work.

Decisions carry fixed reason codes only; request content is never part of them.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Final, Literal

POLICY_VERSION: Final[int] = 1
PROFILE_NORMAL: Final[str] = "normal"
PROFILE_MAINTENANCE: Final[str] = "maintenance"
_PROFILES: Final[tuple[str, ...]] = (PROFILE_NORMAL, PROFILE_MAINTENANCE)
_ALLOWED_METHODS: Final[frozenset[str]] = frozenset({"GET", "POST"})
_HTTPS_PORT: Final[int] = 443
_MAX_ENDPOINTS: Final[int] = 32
_MAX_POLICY_BYTES: Final[int] = 64 * 1024
ID_SEGMENT: Final[str] = "{id}"

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_LITERAL_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DYNAMIC_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_QUERY_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_QUERY_VALUE = re.compile(r"^[A-Za-z0-9_.-]{0,64}$")
_ENDPOINT_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTENT_TYPE = re.compile(r"^[a-z]+/[a-z0-9.+-]+$")
_FORBIDDEN_PATH_CHARS = re.compile(r"[\x00-\x20\x7f\\#]")
_ENCODED_PATH_TRICKS = re.compile(r"%(2e|2f|5c|00)", re.IGNORECASE)

Verdict = Literal["allow", "deny"]


class EgressPolicyInvalid(Exception):
    """The policy file is unusable; the proxy must not start. Reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class Endpoint:
    endpoint_id: str
    host: str
    port: int
    methods: frozenset[str]
    path: tuple[str, ...]
    query: dict[str, None]
    content_types: frozenset[str]


@dataclass(frozen=True)
class EgressPolicy:
    sha256: str
    profiles: dict[str, tuple[Endpoint, ...]]

    def endpoints(self, profile: str) -> tuple[Endpoint, ...]:
        if profile not in self.profiles:
            raise EgressPolicyInvalid("unknown_profile")
        return self.profiles[profile]

    def hosts(self, profile: str) -> frozenset[str]:
        return frozenset(endpoint.host for endpoint in self.endpoints(profile))


@dataclass(frozen=True)
class EgressRequest:
    """Every identity one outbound request carries, as the proxy observed it."""

    method: str
    scheme: str
    connect_host: str
    connect_port: int
    sni: str
    host_header: str
    target: str
    upgrade: bool
    content_type: str | None


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason_code: str
    endpoint_id: str | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


def _deny(reason_code: str) -> Decision:
    return Decision("deny", reason_code)


def policy_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_keys(value: dict[str, Any], keys: set[str], reason: str) -> None:
    if set(value) != keys:
        raise EgressPolicyInvalid(reason)


def _parse_path(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.startswith("/") or raw.endswith("/"):
        raise EgressPolicyInvalid("endpoint_path_invalid")
    segments = tuple(raw.split("/")[1:])
    for segment in segments:
        if segment != ID_SEGMENT and not _LITERAL_SEGMENT.fullmatch(segment):
            raise EgressPolicyInvalid("endpoint_path_invalid")
        if segment in {".", ".."}:
            raise EgressPolicyInvalid("endpoint_path_invalid")
    return segments


def _parse_endpoint(raw: object) -> Endpoint:
    if not isinstance(raw, dict):
        raise EgressPolicyInvalid("endpoint_invalid")
    _require_keys(
        raw,
        {"id", "host", "port", "methods", "path", "query_keys", "content_types"},
        "endpoint_fields_invalid",
    )
    host, port = raw["host"], raw["port"]
    if not isinstance(host, str) or not _HOST.fullmatch(host):
        raise EgressPolicyInvalid("endpoint_host_invalid")
    if port != _HTTPS_PORT:
        raise EgressPolicyInvalid("endpoint_port_invalid")
    methods, keys, types = raw["methods"], raw["query_keys"], raw["content_types"]
    if not (
        isinstance(methods, list)
        and methods
        and set(methods) <= _ALLOWED_METHODS
        and len(set(methods)) == len(methods)
    ):
        raise EgressPolicyInvalid("endpoint_methods_invalid")
    if not (
        isinstance(keys, list)
        and all(isinstance(k, str) and _QUERY_KEY.fullmatch(k) for k in keys)
    ):
        raise EgressPolicyInvalid("endpoint_query_invalid")
    if not (
        isinstance(types, list)
        and all(isinstance(t, str) and _CONTENT_TYPE.fullmatch(t) for t in types)
    ):
        raise EgressPolicyInvalid("endpoint_content_types_invalid")
    endpoint_id = raw["id"]
    if not isinstance(endpoint_id, str) or not _ENDPOINT_ID.fullmatch(endpoint_id):
        raise EgressPolicyInvalid("endpoint_id_invalid")
    return Endpoint(
        endpoint_id=endpoint_id,
        host=host,
        port=port,
        methods=frozenset(methods),
        path=_parse_path(raw["path"]),
        query=dict.fromkeys(keys),
        content_types=frozenset(types),
    )


def _parse_profile(raw: object, *, name: str) -> tuple[Endpoint, ...]:
    if not isinstance(raw, list) or len(raw) > _MAX_ENDPOINTS:
        raise EgressPolicyInvalid("profile_invalid")
    endpoints = tuple(_parse_endpoint(item) for item in raw)
    if len({e.endpoint_id for e in endpoints}) != len(endpoints):
        raise EgressPolicyInvalid("endpoint_id_duplicate")
    if name == PROFILE_NORMAL and not endpoints:
        raise EgressPolicyInvalid("normal_profile_empty")
    return endpoints


def load_policy(raw: bytes, *, expected_sha256: str) -> EgressPolicy:
    """Parse and validate the policy bytes against the reviewed digest.

    Any problem, including an empty file, an unknown field or a digest that is not
    the reviewed one, raises ``EgressPolicyInvalid`` so the proxy never starts.
    """
    if not raw.strip() or len(raw) > _MAX_POLICY_BYTES:
        raise EgressPolicyInvalid("policy_empty_or_oversized")
    digest = policy_sha256(raw)
    if not expected_sha256 or digest != expected_sha256.lower():
        raise EgressPolicyInvalid("policy_digest_mismatch")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise EgressPolicyInvalid("policy_not_json") from exc
    if not isinstance(document, dict):
        raise EgressPolicyInvalid("policy_shape_invalid")
    _require_keys(document, {"version", "profiles"}, "policy_fields_invalid")
    if document["version"] != POLICY_VERSION:
        raise EgressPolicyInvalid("policy_version_unsupported")
    profiles = document["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(_PROFILES):
        raise EgressPolicyInvalid("policy_profiles_invalid")
    return EgressPolicy(
        sha256=digest,
        profiles={
            name: _parse_profile(profiles[name], name=name) for name in _PROFILES
        },
    )


def is_ip_literal(host: str) -> bool:
    """True for a numeric address, including bracketed IPv6.

    Numeric shorthands such as ``2130706433`` or ``0x7f.1`` fail the strict host
    pattern in ``_canonical_host`` (no alphabetic top-level label), so they are
    refused there rather than guessed at here.
    """
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _canonical_host(host: str) -> str | None:
    if not host or host != host.lower() or host.endswith(".") or "@" in host:
        return None
    if is_ip_literal(host) or not _HOST.fullmatch(host):
        return None
    return host


def split_authority(authority: str) -> tuple[str, int] | None:
    """``host[:port]`` -> parts; userinfo, bad ports and bare IPv6 are refused."""
    if not authority or "@" in authority or authority.startswith("["):
        return None
    host, sep, port = authority.partition(":")
    if not sep:
        return host, _HTTPS_PORT
    if not port.isdigit() or len(port) > 5 or port != str(int(port)):
        return None
    return host, int(port)


def _split_target(target: str) -> tuple[str, dict[str, str]] | None:
    if not target.startswith("/") or target.startswith("//"):
        return None
    path, _, query = target.partition("?")
    if _FORBIDDEN_PATH_CHARS.search(target) or _ENCODED_PATH_TRICKS.search(path):
        return None
    if "//" in path or any(seg in {".", ".."} for seg in path.split("/")):
        return None
    pairs: dict[str, str] = {}
    for item in filter(None, query.split("&")):
        key, _, value = item.partition("=")
        if key in pairs:
            return None
        pairs[key] = value
    return path, pairs


def _path_matches(shape: tuple[str, ...], path: str) -> bool:
    actual = path.split("/")[1:]
    if len(actual) != len(shape):
        return False
    return all(
        _DYNAMIC_SEGMENT.fullmatch(got) if want == ID_SEGMENT else got == want
        for want, got in zip(shape, actual, strict=True)
    )


def _query_allowed(endpoint: Endpoint, query: dict[str, str]) -> bool:
    return all(
        key in endpoint.query and _QUERY_VALUE.fullmatch(value)
        for key, value in query.items()
    )


def decide_connect(
    policy: EgressPolicy, profile: str, host: str, port: int
) -> Decision:
    """Whether a CONNECT may be intercepted: an approved name on the TLS port.

    There is no opaque tunnel: every allowed CONNECT is terminated and inspected.
    """
    canonical = _canonical_host(host)
    if canonical is None:
        return _deny("host_not_canonical")
    if port != _HTTPS_PORT:
        return _deny("port_not_allowed")
    if canonical not in policy.hosts(profile):
        return _deny("host_not_allowed")
    return Decision("allow", "connect_allowed")


def _authority_decision(request: EgressRequest) -> Decision | None:
    if request.scheme != "https":
        return _deny("scheme_not_https")
    host = _canonical_host(request.connect_host)
    if host is None:
        return _deny("host_not_canonical")
    header = split_authority(request.host_header)
    if header is None:
        return _deny("authority_invalid")
    if (
        request.sni != host
        or header[0] != host
        or header[1] != request.connect_port
    ):
        return _deny("authority_mismatch")
    if request.upgrade:
        return _deny("upgrade_not_allowed")
    return None


def decide_request(
    policy: EgressPolicy, profile: str, request: EgressRequest
) -> Decision:
    """Allow a request only when it matches one endpoint of the profile exactly."""
    if (refused := _authority_decision(request)) is not None:
        return refused
    if request.connect_port != _HTTPS_PORT:
        return _deny("port_not_allowed")
    candidates = [
        endpoint
        for endpoint in policy.endpoints(profile)
        if endpoint.host == request.connect_host
    ]
    if not candidates:
        return _deny("host_not_allowed")
    split = _split_target(request.target)
    if split is None:
        return _deny("path_not_canonical")
    path, query = split
    for endpoint in candidates:
        if not _path_matches(endpoint.path, path):
            continue
        if request.method not in endpoint.methods:
            return _deny("method_not_allowed")
        if not _query_allowed(endpoint, query):
            return _deny("query_not_allowed")
        if request.method == "POST" and (
            request.content_type is None
            or request.content_type.split(";")[0].strip().lower()
            not in endpoint.content_types
        ):
            return _deny("content_type_not_allowed")
        return Decision("allow", "request_allowed", endpoint.endpoint_id)
    return _deny("path_not_allowed")


def decide_response(status: int, *, has_location: bool) -> Decision:
    """Redirects are never followed: the CLI needs none on an approved endpoint."""
    if 300 <= status < 400 or has_location:
        return _deny("redirect_blocked")
    return Decision("allow", "response_allowed")


class NoPermittedAddress(Exception):
    """Name resolution produced no address the proxy may dial."""


def pin_address(resolved: list[str]) -> str:
    """Choose the single address to dial, or refuse.

    Only global unicast IPv4 addresses qualify: private, loopback, link-local,
    metadata, multicast, reserved and every IPv6 address (including IPv4-mapped
    forms) are dropped, so a rebinding answer cannot steer the connection inward.
    The caller dials exactly the returned address and never resolves again.
    """
    permitted: list[str] = []
    for text in resolved:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address.version == 4 and address.is_global and not address.is_multicast:
            permitted.append(str(address))
    if not permitted or len(permitted) != len(resolved):
        raise NoPermittedAddress
    return permitted[0]
