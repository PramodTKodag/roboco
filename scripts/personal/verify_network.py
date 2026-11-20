"""Prove the egress boundary from the exact runtime and worker network namespaces.

Run against a started personal stack (``compose.personal.yml``). Probes use
synthetic data only and contact no real provider: every request here is one the
policy must refuse, and every direct route must be missing. The approved
inference request is verified separately, by the owner, with redacted evidence.

    python scripts/personal/verify_network.py --project roboco-personal \
        --service-image roboco-personal-claude-service:itest \
        --worker-image roboco-personal-worker:itest

Exit status 0 only when every check passed. Output is a JSON list of
``{name, passed, detail}``; ``detail`` is a fixed code, never traffic content.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from string import Template
from uuid import uuid4

PROBE_LABEL = "roboco.personal.network-probe"
CANARY = "synthetic-network-canary-4b7a"
COLLECTOR_PORT = 9999
PROXY_SERVICE = "egress-proxy"
PROXY_PORT = 8080
CA_PATH = "/ca/mitmproxy-ca-cert.pem"

_PROBE = Template(
    textwrap.dedent(
        """
    import http.client, json, socket, ssl, sys

    mode, proxy, collector, canary = sys.argv[1:5]

    def connects(address):
        try:
            socket.create_connection(address, timeout=4).close()
            return True
        except OSError:
            return False

    def tunnel(host, method, target):
        context = ssl.create_default_context(cafile="$ca")
        try:
            conn = http.client.HTTPSConnection(
                proxy, $port, context=context, timeout=15
            )
            conn.set_tunnel(host, 443)
            conn.request(method, target, body=canary, headers={"X-Canary": canary})
            return conn.getresponse().status
        except OSError as exc:
            return "error:" + type(exc).__name__ + ":" + str(exc)[:40]

    out = {}
    if mode == "runtime":
        out["telemetry_via_proxy"] = tunnel(
            "api.anthropic.com", "POST", "/api/event_logging/v2/batch")
        out["collector_via_proxy"] = tunnel(collector, "POST", "/")
    out["direct_ipv4"] = connects(("1.1.1.1", 443))
    out["direct_ipv6"] = connects(("2606:4700:4700::1111", 443))
    out["direct_dns_server"] = connects(("8.8.8.8", 53))
    try:
        socket.getaddrinfo("example.com", 443)
        out["external_dns"] = True
    except OSError:
        out["external_dns"] = False
    try:
        socket.getaddrinfo("host.docker.internal", 443)
        out["host_gateway_name"] = True
    except OSError:
        out["host_gateway_name"] = False
    out["collector_direct"] = connects((collector, $collector_port))
    out["proxy_reachable"] = connects((proxy, $port))
    print(json.dumps(out))
    """
    )
).substitute(ca=CA_PATH, port=PROXY_PORT, collector_port=COLLECTOR_PORT)

_COLLECTOR = Template(
    textwrap.dedent(
        """
    import socket, sys
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", $port))
    server.listen()
    print("collector ready", flush=True)
    while True:
        conn, _ = server.accept()
        print("collector connection", flush=True)
        conn.close()
    """
    )
).substitute(port=COLLECTOR_PORT)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=300,
    )


def _project_container(project: str, service: str) -> str:
    listed = _docker(
        "ps",
        "-a",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={service}",
        "--format",
        "{{.Names}}",
    ).stdout.split()
    if len(listed) != 1:
        raise SystemExit(f"expected exactly one {service} container in {project}")
    return listed[0]


def _probe(
    mode: str,
    image: str,
    network: str,
    collector: str,
    ca_volume: str | None,
) -> dict[str, object]:
    args = [
        "run",
        "--rm",
        "--label",
        PROBE_LABEL,
        "--network",
        network,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--entrypoint",
        "python",
    ]
    if ca_volume is not None:
        args += ["-v", f"{ca_volume}:/ca:ro"]
    args += [image, "-c", _PROBE, mode, PROXY_SERVICE, collector, CANARY]
    return json.loads(_docker(*args).stdout)


def _expect(name: str, ok: bool, detail: str) -> CheckResult:
    return CheckResult(name, ok, detail if not ok else "ok")


def run_checks(
    project: str, service_image: str, worker_image: str
) -> list[CheckResult]:
    network = f"{project}_runtime"
    ca_volume = f"{project}_egress_ca_public"
    proxy_container = _project_container(project, PROXY_SERVICE)
    suffix = uuid4().hex[:8]
    collector = f"roboco-probe-collector-{suffix}"
    results: list[CheckResult] = []
    _docker(
        "run",
        "-d",
        "--name",
        collector,
        "--label",
        PROBE_LABEL,
        "--network",
        network,
        "--read-only",
        "--cap-drop",
        "ALL",
        "--entrypoint",
        "python",
        service_image,
        "-u",
        "-c",
        _COLLECTOR,
    )
    try:
        time.sleep(1)
        runtime = _probe("runtime", service_image, network, collector, ca_volume)
        worker = _probe("worker", worker_image, "none", collector, None)
        results += _runtime_results(runtime) + _worker_results(worker)
        collected = _docker("logs", collector).stdout.count("collector connection")
        # The runtime's own direct attempt to the collector is the only allowed
        # contact on this internal network; nothing may arrive through the proxy
        # or from a worker. The runtime probe reports whether it connected.
        expected = 1 if runtime["collector_direct"] else 0
        results.append(
            _expect(
                "collector_received_nothing_through_proxy_or_worker",
                collected == expected,
                f"connections={collected}",
            )
        )
        _docker("stop", "--time", "2", proxy_container)
        stopped = _probe("runtime", service_image, network, collector, ca_volume)
        results.append(
            _expect(
                "proxy_stopped_blocks_provider_path",
                not stopped["proxy_reachable"]
                and str(stopped["telemetry_via_proxy"]).startswith("error"),
                "proxy stopped but still reachable",
            )
        )
    finally:
        _docker("rm", "--force", collector, check=False)
        _docker("start", proxy_container, check=False)
    return results


def _runtime_results(probe: dict[str, object]) -> list[CheckResult]:
    telemetry = probe["telemetry_via_proxy"]
    return [
        _expect(
            "runtime_reaches_proxy",
            probe["proxy_reachable"] is True,
            "proxy unreachable",
        ),
        _expect(
            "runtime_telemetry_denied_by_policy",
            telemetry == HTTPStatus.FORBIDDEN,
            str(telemetry)[:60],
        ),
        _expect(
            "runtime_tunnel_to_unapproved_host_refused",
            str(probe["collector_via_proxy"]).startswith("error"),
            "tunnel to collector was accepted",
        ),
        _expect(
            "runtime_no_direct_ipv4",
            probe["direct_ipv4"] is False,
            "direct IPv4 connected",
        ),
        _expect(
            "runtime_no_direct_ipv6",
            probe["direct_ipv6"] is False,
            "direct IPv6 connected",
        ),
        _expect(
            "runtime_no_outside_dns_server",
            probe["direct_dns_server"] is False,
            "outside DNS server reachable",
        ),
        _expect(
            "runtime_no_external_name_resolution",
            probe["external_dns"] is False,
            "external names resolve",
        ),
        _expect(
            "runtime_no_host_gateway_name",
            probe["host_gateway_name"] is False,
            "host gateway name resolves",
        ),
    ]


def _worker_results(probe: dict[str, object]) -> list[CheckResult]:
    return [
        _expect(f"worker_{key}", probe[key] is False, "reachable")
        for key in (
            "proxy_reachable",
            "collector_direct",
            "direct_ipv4",
            "direct_ipv6",
            "direct_dns_server",
            "external_dns",
            "host_gateway_name",
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--service-image", required=True)
    parser.add_argument("--worker-image", required=True)
    arguments = parser.parse_args()
    results = run_checks(
        arguments.project, arguments.service_image, arguments.worker_image
    )
    print(json.dumps([asdict(result) for result in results], indent=2))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
