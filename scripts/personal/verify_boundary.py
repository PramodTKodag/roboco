"""Check the running personal stack's security boundary from the outside.

Reads only container metadata and sends synthetic requests; it never opens a
credential volume, reads an environment value or contacts a provider. Every
direct-egress probe must FAIL for the check to pass.

    python scripts/personal/verify_boundary.py [--project roboco-personal]

Output is a JSON list of ``{name, passed, detail}`` where ``detail`` is a fixed
code. Exit status is 0 only when every check passed.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess  # nosec B404 - fixed argv lists only, never a shell
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

import stack

from roboco.foundation.policy.subscription_policy import forbidden_env_names

LAUNCHER = "launcher"
EDGE = "nginx"
PROXY = "egress-proxy"
CLAUDE_SERVICE = "claude-service"
CREDENTIAL_VOLUME = "claude_credentials"
DOCKER_SOCKET = "/var/run/docker.sock"
# The only capabilities a service may add back after dropping ALL, mirroring
# compose.personal.yml: datastore entrypoints chown their data directory and
# drop to their own user, and the edge binds its port.
EXTRA_CAPABILITIES = {
    "postgres": {"CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID"},
    "redis": {"CHOWN", "SETGID", "SETUID"},
    "nginx": {"CHOWN", "DAC_OVERRIDE", "NET_BIND_SERVICE", "SETGID", "SETUID"},
    "egress-ca-publish": {"DAC_OVERRIDE"},
}
CONTROLLERS = ("orchestrator", "dispatcher")
DIRECT_ROUTE_PROBE = (
    "import socket,sys\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 443), timeout=5).close()\n"
    "    print('connected')\n"
    "except OSError:\n"
    "    print('blocked')\n"
)
NAME_RESOLUTION_PROBE = (
    "import socket\n"
    "try:\n"
    "    socket.getaddrinfo('example.com', 443)\n"
    "    print('resolved')\n"
    "except OSError:\n"
    "    print('blocked')\n"
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def _docker(*args: str) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout


def _service_of(container: dict[str, Any]) -> str:
    return str(container["Config"]["Labels"]["com.docker.compose.service"])


def inspect_project(project: str) -> dict[str, dict[str, Any]]:
    """Running containers of the project, keyed by compose service name."""
    ids = _docker(
        "ps", "--filter", f"label=com.docker.compose.project={project}", "--quiet"
    ).split()
    if not ids:
        return {}
    containers = json.loads(_docker("inspect", *ids))
    return {_service_of(container): container for container in containers}


def _check(name: str, offenders: list[str]) -> Check:
    return Check(name, not offenders, "ok" if not offenders else ",".join(offenders))


def check_isolation_flags(containers: dict[str, dict[str, Any]]) -> list[Check]:
    privileged, host_namespace, caps, escalation = [], [], [], []
    for service, container in containers.items():
        host = container["HostConfig"]
        if host.get("Privileged"):
            privileged.append(service)
        if "host" in (
            host.get("NetworkMode"),
            host.get("PidMode"),
            host.get("IpcMode"),
        ):
            host_namespace.append(service)
        added = {cap.removeprefix("CAP_") for cap in host.get("CapAdd") or []}
        if "ALL" not in (
            host.get("CapDrop") or []
        ) or not added <= EXTRA_CAPABILITIES.get(service, set()):
            caps.append(service)
        if "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
            escalation.append(service)
    return [
        _check("no_privileged_container", privileged),
        _check("no_host_namespace", host_namespace),
        _check("all_capabilities_dropped", caps),
        _check("no_new_privileges", escalation),
    ]


def _mount_sources(container: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (str(mount.get("Name") or mount.get("Source")), str(mount["Destination"]))
        for mount in container["Mounts"]
    ]


def check_mounts(containers: dict[str, dict[str, Any]]) -> list[Check]:
    socket_holders = sorted(
        service
        for service, container in containers.items()
        if any(DOCKER_SOCKET in source for source, _ in _mount_sources(container))
    )
    credential_holders = sorted(
        service
        for service, container in containers.items()
        if any(CREDENTIAL_VOLUME in source for source, _ in _mount_sources(container))
    )
    home_binds = sorted(
        service
        for service, container in containers.items()
        for mount in container["Mounts"]
        if mount["Type"] == "bind"
        and mount["Source"] != DOCKER_SOCKET
        and str(stack.REPO_ROOT) not in mount["Source"]
    )
    return [
        Check(
            "docker_socket_only_in_launcher",
            socket_holders == [LAUNCHER],
            ",".join(socket_holders),
        ),
        Check(
            "credential_volume_only_in_claude_service",
            credential_holders == [CLAUDE_SERVICE],
            ",".join(credential_holders),
        ),
        _check("no_bind_outside_the_repository_tree", home_binds),
    ]


def check_environment_names(containers: dict[str, dict[str, Any]]) -> Check:
    offenders = []
    for service, container in containers.items():
        names = {
            entry.split("=", 1)[0]: "" for entry in container["Config"]["Env"] or []
        }
        if forbidden_env_names(names):
            offenders.append(service)
    return _check("no_provider_credential_variable_in_any_container", offenders)


def check_exposure(containers: dict[str, dict[str, Any]], project: str) -> list[Check]:
    published = {
        service: bindings
        for service, container in containers.items()
        if (
            bindings := {
                port: binds
                for port, binds in (container["NetworkSettings"]["Ports"] or {}).items()
                if binds
            }
        )
    }
    loopback_only = all(
        bind["HostIp"] == "127.0.0.1"
        for binds in published.values()
        for entries in binds.values()
        for bind in entries
    )
    egress_members = sorted(
        service
        for service, container in containers.items()
        if f"{project}_egress" in container["NetworkSettings"]["Networks"]
    )
    return [
        Check(
            "only_the_edge_publishes_a_port",
            list(published) == [EDGE],
            ",".join(published),
        ),
        Check(
            "published_port_is_loopback_only",
            loopback_only,
            "ok" if loopback_only else "public_bind",
        ),
        Check(
            "only_the_proxy_joins_the_internet_network",
            egress_members == [PROXY],
            ",".join(egress_members),
        ),
    ]


def check_no_direct_route(containers: dict[str, dict[str, Any]]) -> list[Check]:
    results = []
    for service in (*CONTROLLERS, CLAUDE_SERVICE):
        if service not in containers:
            results.append(
                Check(f"{service}_has_no_direct_route", False, "not_running")
            )
            continue
        for name, probe in (
            ("route", DIRECT_ROUTE_PROBE),
            ("name_resolution", NAME_RESOLUTION_PROBE),
        ):
            outcome = subprocess.run(  # noqa: S603  # nosec B603
                ["docker", "exec", containers[service]["Id"], "python", "-c", probe],  # noqa: S607
                capture_output=True, text=True, check=False, timeout=30,
            ).stdout.strip()  # fmt: skip
            results.append(
                Check(
                    f"{service}_has_no_direct_{name}",
                    outcome == "blocked",
                    outcome or "no_output",
                )
            )
    return results


def _status(
    url: str, *, host: str | None = None, method: str = "GET", opener: Any = None
) -> int:
    request = urllib.request.Request(url, method=method)  # noqa: S310  # nosec B310
    if host:
        request.add_header("Host", host)
    try:
        return int(
            (opener or urllib.request.build_opener()).open(request, timeout=15).status
        )
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, http.client.HTTPException):
        return 0


def check_http_boundary() -> list[Check]:
    base = stack.owner_url()
    port = base.rsplit(":", 1)[1]
    anonymous_status = _status(f"{base}/api/tasks")
    wrong_host_status = _status(f"{base}/api/tasks", host="evil.example")
    opener = stack.owner_session()
    cross_origin = urllib.request.Request(  # noqa: S310  # nosec B310
        f"{base}/api/projects", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Origin": "http://evil.example"},
    )  # fmt: skip
    cross_origin_status = 0
    try:
        opener.open(cross_origin, timeout=15)
    except urllib.error.HTTPError as exc:
        cross_origin_status = exc.code
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=15)
    connection.request(
        "GET", "/ws/system",
        headers={"Host": f"localhost:{port}", "Upgrade": "websocket", "Connection": "Upgrade",
                 "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "c3ludGhldGljLWtleS0xMjM0NQ=="},
    )  # fmt: skip
    websocket_status = connection.getresponse().status
    flags = json.load(opener.open(f"{base}/api/settings/feature-flags", timeout=15))[
        "flags"
    ]
    unlocked_forbidden = [
        flag["key"] for flag in flags if flag["locked"] and flag["enabled"]
    ]
    return [
        Check(
            "anonymous_api_request_is_refused",
            anonymous_status == 401,
            str(anonymous_status),
        ),
        Check(
            "wrong_host_is_refused",
            wrong_host_status in {0, 421},
            str(wrong_host_status),
        ),
        Check(
            "cross_origin_write_with_session_is_refused",
            cross_origin_status == 403,
            str(cross_origin_status),
        ),
        Check(
            "anonymous_websocket_is_refused",
            websocket_status != 101,
            str(websocket_status),
        ),
        _check("locked_feature_flags_are_off", unlocked_forbidden),
    ]


def run_checks(project: str) -> list[Check]:
    containers = inspect_project(project)
    if not containers:
        return [Check("stack_is_running", False, "no_containers")]
    return [
        Check("stack_is_running", True, f"{len(containers)}_services"),
        *check_isolation_flags(containers),
        *check_mounts(containers),
        check_environment_names(containers),
        *check_exposure(containers, project),
        *check_no_direct_route(containers),
        *check_http_boundary(),
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="roboco-personal")
    args = parser.parse_args(argv[1:])
    stack.refuse_inherited_credentials(os.environ)
    results = run_checks(args.project)
    print(json.dumps([asdict(result) for result in results], indent=1))
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
