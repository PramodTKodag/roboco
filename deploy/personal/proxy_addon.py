"""mitmproxy addon: forward only what the reviewed egress policy allows.

Runs inside the pinned proxy container with the policy module mounted beside it.
Every decision is made by ``egress_policy``; this file only adapts mitmproxy's
events to it. Logs carry reason codes and endpoint ids, never a URL, header,
query value or body, and no flow is ever written to disk.

A policy that cannot be loaded, or is not the reviewed digest, ends the process:
the proxy never runs with a default or partial policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path
from typing import Any

from egress_policy import (
    Decision,
    EgressPolicy,
    EgressPolicyInvalid,
    EgressRequest,
    NoPermittedAddress,
    decide_connect,
    decide_request,
    decide_response,
    load_policy,
    pin_address,
)
from mitmproxy import http

logger = logging.getLogger("roboco.egress")

POLICY_PATH_ENV = "ROBOCO_EGRESS_POLICY_PATH"
POLICY_SHA256_ENV = "ROBOCO_EGRESS_POLICY_SHA256"
PROFILE_ENV = "ROBOCO_EGRESS_PROFILE"
EXIT_POLICY_INVALID = 78
_DENY_STATUS = 403
_UPGRADE_HEADERS = ("upgrade", "http2-settings")


def _refuse_to_start(reason_code: str) -> None:
    logger.error("egress policy rejected at startup: %s", reason_code)
    sys.stderr.write(f"egress policy rejected: {reason_code}\n")
    sys.stderr.flush()
    os._exit(EXIT_POLICY_INVALID)


def _reply(status: int, reason_code: str) -> http.Response:
    body = json.dumps({"error": reason_code}).encode()
    return http.Response.make(status, body, {"Content-Type": "application/json"})


def _wants_upgrade(headers: Any) -> bool:
    if any(name in headers for name in _UPGRADE_HEADERS):
        return True
    return "upgrade" in headers.get("connection", "").lower()


class EgressGate:
    """Applies one immutable policy and profile to every flow."""

    def __init__(self) -> None:
        self._policy: EgressPolicy | None = None
        self._profile = ""
        self._connect_hosts: dict[str, str] = {}

    def load(self, loader: Any) -> None:
        del loader
        try:
            raw = Path(os.environ[POLICY_PATH_ENV]).read_bytes()
            self._policy = load_policy(
                raw, expected_sha256=os.environ.get(POLICY_SHA256_ENV, "")
            )
            self._profile = os.environ[PROFILE_ENV]
            self._policy.endpoints(self._profile)
        except (KeyError, OSError):
            _refuse_to_start("policy_unreadable")
        except EgressPolicyInvalid as exc:
            _refuse_to_start(exc.reason_code)
        logger.info(
            "egress policy loaded profile=%s sha256=%s",
            self._profile,
            self._policy.sha256 if self._policy else "",
        )

    def _log(self, decision: Decision, stage: str, method: str = "") -> None:
        logger.info(
            "egress %s %s %s endpoint=%s reason=%s",
            decision.verdict,
            stage,
            method,
            decision.endpoint_id or "-",
            decision.reason_code,
        )

    def http_connect(self, flow: http.HTTPFlow) -> None:
        assert self._policy is not None
        host, port = flow.request.host, flow.request.port
        decision = decide_connect(self._policy, self._profile, host, port)
        self._log(decision, "connect")
        if not decision.allowed:
            flow.response = _reply(_DENY_STATUS, decision.reason_code)
            return
        self._connect_hosts[flow.client_conn.id] = host

    async def server_connect(self, data: Any) -> None:
        """Resolve once, check every answer, and dial exactly that address."""
        host, port = data.server.address
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
            pinned = pin_address([info[4][0] for info in infos])
        except (OSError, NoPermittedAddress):
            logger.info("egress deny dial endpoint=- reason=resolution_refused")
            data.server.error = "egress: destination address refused"
            return
        data.server.address = (pinned, port)
        data.server.sni = host

    def request(self, flow: http.HTTPFlow) -> None:
        assert self._policy is not None
        connect_host = self._connect_hosts.get(flow.client_conn.id, "")
        egress = EgressRequest(
            method=flow.request.method,
            scheme=flow.request.scheme,
            connect_host=connect_host,
            connect_port=flow.request.port,
            sni=flow.client_conn.sni or "",
            host_header=flow.request.headers.get("host", ""),
            target=flow.request.path,
            upgrade=_wants_upgrade(flow.request.headers),
            content_type=flow.request.headers.get("content-type"),
        )
        decision = decide_request(self._policy, self._profile, egress)
        self._log(decision, "request", flow.request.method)
        if not decision.allowed:
            flow.response = _reply(_DENY_STATUS, decision.reason_code)

    def response(self, flow: http.HTTPFlow) -> None:
        assert flow.response is not None
        decision = decide_response(
            flow.response.status_code,
            has_location="location" in flow.response.headers,
        )
        if not decision.allowed:
            self._log(decision, "response")
            flow.response = _reply(_DENY_STATUS, decision.reason_code)

    def error(self, flow: http.HTTPFlow) -> None:
        logger.info("egress upstream error endpoint=- reason=upstream_unavailable")
        del flow

    def client_disconnected(self, client: Any) -> None:
        self._connect_hosts.pop(client.id, None)

    def tcp_start(self, flow: Any) -> None:
        logger.info("egress deny tcp endpoint=- reason=unexpected_protocol")
        flow.kill()

    def udp_start(self, flow: Any) -> None:
        logger.info("egress deny udp endpoint=- reason=unexpected_protocol")
        flow.kill()

    def websocket_start(self, flow: Any) -> None:
        logger.info("egress deny websocket endpoint=- reason=upgrade_not_allowed")
        flow.kill()

    def dns_request(self, flow: Any) -> None:
        logger.info("egress deny dns endpoint=- reason=unexpected_protocol")
        flow.kill()


addons = [EgressGate()]
