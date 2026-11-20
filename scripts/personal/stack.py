"""Owner-side control of the personal deployment (run through ``make personal-*``).

Every command first refuses to run if the host environment carries a provider
credential or override, then drives ``compose.personal.yml`` with generated
state. Nothing here reads the owner's Claude configuration or Keychain, deletes a
volume, or logs an account out. The subscription login lives only in the
``claude_credentials`` volume, written by the owner's own terminal login.

Commands: stage, up, status, login, check, import, export, stop.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import secrets
import subprocess  # nosec B404 - fixed argv lists only, never a shell
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from uuid import UUID
from pathlib import Path

import probe_cli_contract as cli_contract

from roboco.foundation.policy.subscription_policy import (
    classify_auth_status_text,
    forbidden_env_names,
)
from roboco.runtime.subscription.policy import build_claude_env, parse_egress_route

logger = logging.getLogger("personal_stack")

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = REPO_ROOT / ".personal"
COMPOSE_FILE = REPO_ROOT / "compose.personal.yml"
POLICY_FILE = REPO_ROOT / "deploy" / "personal" / "egress-policy.json"
POLICY_DIGEST_FILE = REPO_ROOT / "deploy" / "personal" / "egress-policy.sha256"
WORKER_IMAGE_SERVICE = "worker-image"
SERVICE_CLI = "/opt/claude/claude"
DEFAULT_PORT = "3300"
OWNER_EMAIL = "owner@localhost.localdomain"
TOKEN_NAMES = ("claude_service_token", "launcher_admin_token", "launcher_executor_token")
IMPORT_SCRIPT = REPO_ROOT / "scripts" / "personal" / "import_workspace.py"
IMPORT_MOUNT = "/import/source"
HANDLE_LINE = re.compile(r"^workspace handle: ([0-9a-f-]{36})$", re.MULTILINE)
EGRESS_ROUTE = parse_egress_route(
    "http://egress-proxy:8080", "/etc/roboco/egress/mitmproxy-ca-cert.pem"
)


class StackRefused(Exception):
    """A preflight check failed; the message names the cause, never a secret."""


def refuse_inherited_credentials(environ: dict[str, str] | os._Environ[str]) -> None:
    names = forbidden_env_names(environ)  # type: ignore[arg-type]
    if names:
        raise StackRefused(
            "refusing to run: provider credential or override variables are set in "
            "this shell: " + ", ".join(names)
        )


def owner_url() -> str:
    """The published panel origin; the port is the owner's single setting."""
    port = os.environ.get("ROBOCO_PERSONAL_PORT", DEFAULT_PORT)
    if not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise StackRefused("ROBOCO_PERSONAL_PORT must be a port between 1024 and 65535")
    return f"http://localhost:{port}"


def _write_private(path: Path, text: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(mode)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def ensure_state(state: Path = STATE_DIR) -> None:
    """Create the secrets once; an existing value is never rotated or overwritten."""
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    secrets_dir = state / "secrets"
    secrets_dir.mkdir(mode=0o700, exist_ok=True)
    for name in TOKEN_NAMES:
        token = secrets_dir / name
        if not token.exists():
            # Readable by the service's unprivileged user; the directory is 0700.
            _write_private(token, secrets.token_urlsafe(48), 0o444)
    env_file = state / "secrets.env"
    if env_file.exists():
        return
    panel_password = secrets.token_urlsafe(24)
    values = {
        "ROBOCO_DATABASE_PASSWORD": secrets.token_urlsafe(32),
        "ROBOCO_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "ROBOCO_AGENT_AUTH_SECRET": secrets.token_hex(32),
        "ROBOCO_CLOUD_AUTH_SECRET": secrets.token_hex(32),
        "ROBOCO_CLOUD_AUTH_EMAIL": OWNER_EMAIL,
        "ROBOCO_CLOUD_AUTH_PASSWORD": panel_password,
    }
    _write_private(
        env_file, "".join(f"{k}={v}\n" for k, v in values.items()), 0o600
    )
    _write_private(
        state / "panel-login.txt",
        f"Panel login (local only)\nemail: {OWNER_EMAIL}\npassword: {panel_password}\n",
        0o600,
    )


def policy_digest() -> str:
    """The reviewed digest, refused unless it matches the policy file's bytes."""
    recorded = POLICY_DIGEST_FILE.read_text(encoding="utf-8").split()[0]
    actual = hashlib.sha256(POLICY_FILE.read_bytes()).hexdigest()
    if recorded != actual:
        raise StackRefused(
            "egress policy does not match its reviewed digest; review the change "
            "and update deploy/personal/egress-policy.sha256"
        )
    return recorded


def staged_cli_context() -> tuple[Path, str]:
    """The verified CLI directory and its pinned digest, or a refusal."""
    arch = cli_contract.host_arch()
    pinned = cli_contract.PINNED_BINARY_SHA256[arch]
    if cli_contract.staged_binary_sha256(arch) != pinned:
        raise StackRefused(
            "the pinned CLI is not staged or does not match its digest; "
            "run `make personal-stage`"
        )
    return cli_contract.staged_binary_path(arch).parent, pinned


def _source_tag() -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603 B607
        ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() or "local"


def write_derived_env(worker_image: str, state: Path = STATE_DIR) -> None:
    context, cli_sha = staged_cli_context()
    values = {
        "ROBOCO_PERSONAL_STATE": str(state),
        "ROBOCO_PERSONAL_TAG": _source_tag(),
        "ROBOCO_CLI_CONTEXT": str(context),
        "ROBOCO_CLI_SHA256": cli_sha,
        "ROBOCO_EGRESS_POLICY_SHA256": policy_digest(),
        "ROBOCO_WORKER_IMAGE": worker_image,
    }
    _write_private(
        state / "derived.env", "".join(f"{k}={v}\n" for k, v in values.items()), 0o600
    )


def _compose_argv(*args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--file",
        str(COMPOSE_FILE),
        "--env-file",
        str(STATE_DIR / "secrets.env"),
        "--env-file",
        str(STATE_DIR / "derived.env"),
        *args,
    ]


def _run(argv: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603
        argv,
        cwd=REPO_ROOT,
        text=True,
        capture_output=capture,
        check=False,
    )


def _compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return _run(_compose_argv(*args), capture=capture)


def _require_ok(result: subprocess.CompletedProcess[str], what: str) -> str:
    if result.returncode != 0:
        raise StackRefused(f"{what} failed")
    return result.stdout or ""


def _built_worker_image() -> str:
    tag = f"roboco-personal-worker:{_source_tag()}"
    inspected = _run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], capture=True)
    return _require_ok(inspected, "reading the worker image id").strip()


def preflight() -> None:
    refuse_inherited_credentials(os.environ)
    ensure_state()
    write_derived_env("sha256:pending")


def stage() -> None:
    refuse_inherited_credentials(os.environ)
    print(cli_contract.stage_pinned_cli())


def up() -> None:
    preflight()
    _require_ok(_compose("--profile", "build", "build"), "building the images")
    write_derived_env(_built_worker_image())
    _require_ok(_compose("up", "--detach", "--wait", "--remove-orphans"), "starting the stack")
    print(f"RoboCo is up at {owner_url()} (panel login: {STATE_DIR / 'panel-login.txt'})")


def stop() -> None:
    """Stop the services; volumes, the login and workspaces are kept."""
    preflight()
    _require_ok(_compose("stop"), "stopping the stack")


def _claude_env_flags() -> list[str]:
    flags: list[str] = []
    for name, value in build_claude_env(EGRESS_ROUTE).items():
        flags += ["--env", f"{name}={value}"]
    return flags


def subscription_state() -> str:
    """The login classification only; no account identifier ever leaves the service."""
    result = _compose(
        "exec", "--no-TTY", *_claude_env_flags(), "claude-service",
        SERVICE_CLI, "auth", "status", "--json",
        capture=True,
    )  # fmt: skip
    return classify_auth_status_text(result.stdout) if result.returncode == 0 else "login_required"


def status() -> None:
    preflight()
    print(_compose("ps", capture=True).stdout)
    print(f"subscription: {subscription_state()}")


DENIED_TARGET = re.compile(r"egress deny \S+ .*? target=(\S+)")


def denied_targets(proxy_log: str) -> list[str]:
    """Distinct refused destinations, in first-seen order, from maintenance logs."""
    seen: dict[str, None] = {}
    for line in proxy_log.splitlines():
        match = DENIED_TARGET.search(line)
        if match:
            seen.setdefault(match.group(1))
    return list(seen)


def _report_denied_targets(targets: list[str]) -> None:
    if not targets:
        print("no refused destinations were recorded during the login")
        return
    print("destinations the login tried and the proxy refused:")
    for target in targets:
        print(f"  {target}")
    print(
        "review each one; approve only what the sign-in needs by adding an exact "
        "entry to the maintenance profile in deploy/personal/egress-policy.json "
        "and updating deploy/personal/egress-policy.sha256, then run "
        "`make personal-login` again (see docs/personal/owner-setup.md)"
    )


def _proxy_log_since(started_at: str) -> str:
    return _compose(
        "logs", "--no-color", "--since", started_at, "egress-proxy", capture=True
    ).stdout


def login() -> None:
    """Owner-terminal login through the maintenance egress profile.

    The maintenance profile is the owner's reviewed allowlist (empty until the
    owner approves the sign-in endpoints). Model runs are paused for the window
    and the normal profile is always restored.
    """
    preflight()
    _require_ok(_compose("stop", "dispatcher"), "pausing model runs")
    env = {**os.environ, "ROBOCO_EGRESS_PROFILE": "maintenance"}
    started_at = datetime.now(UTC).isoformat()
    targets: list[str] = []
    try:
        _require_ok(
            subprocess.run(  # noqa: S603  # nosec B603
                _compose_argv("up", "--detach", "--wait", "--force-recreate", "egress-proxy"),
                cwd=REPO_ROOT, env=env, check=False, text=True,
            ),
            "switching the egress proxy to the login profile",
        )  # fmt: skip
        _compose(
            "run", "--rm", "--no-deps", "--entrypoint", SERVICE_CLI,
            *_claude_env_flags(), "claude-service", "auth", "login",
        )  # fmt: skip
        targets = denied_targets(_proxy_log_since(started_at))
    finally:
        _compose("up", "--detach", "--wait", "--force-recreate", "egress-proxy")
        _compose("up", "--detach", "--wait", "dispatcher")
    _report_denied_targets(targets)
    print(f"subscription: {subscription_state()}")


def check() -> None:
    preflight()
    _require_ok(_compose("--profile", "build", "config", "--quiet"), "validating the compose file")
    tests = _run(
        [sys.executable, "-m", "pytest", "tests/unit/subscription", "tests/security/subscription",
         "--no-cov", "-p", "no:cacheprovider", "-q"]
    )  # fmt: skip
    if tests.returncode != 0:
        raise StackRefused("the subscription test suites failed")


def _owner_session() -> urllib.request.OpenerDirector:
    password = _read_env_file(STATE_DIR / "secrets.env")["ROBOCO_CLOUD_AUTH_PASSWORD"]
    # The edge is plain HTTP on loopback only; the session cookie is `Secure`, so
    # the jar must be told that loopback HTTP may carry it.
    jar = http.cookiejar.CookieJar(
        http.cookiejar.DefaultCookiePolicy(secure_protocols=("https", "http"))
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # The local access guard checks Origin on state-changing calls, as it does
    # for the owner's browser.
    opener.addheaders = [("Origin", owner_url())]
    form = urllib.parse.urlencode({"username": OWNER_EMAIL, "password": password}).encode()
    opener.open(urllib.request.Request(f"{owner_url()}/api/auth/login", data=form), timeout=15)  # noqa: S310  # nosec B310
    return opener


def validate_import_source(source: Path) -> Path:
    """An existing directory that is not the home directory or any parent of it."""
    resolved = source.expanduser().resolve()
    if not resolved.is_dir():
        raise StackRefused("the import source is not a directory")
    home = Path.home().resolve()
    if resolved == home or resolved in home.parents:
        raise StackRefused(
            "refusing to import a home directory or one of its parents; "
            "choose the project directory itself"
        )
    return resolved


def _link_workspace(project_id: UUID, handle: str) -> None:
    opener = _owner_session()
    request = urllib.request.Request(  # noqa: S310
        f"{owner_url()}/api/projects/{project_id}/offline-workspace",
        data=json.dumps({"workspace_handle": handle}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    opener.open(request, timeout=30)  # noqa: S310  # nosec B310


def import_project(
    project_id: UUID, source: Path, options: list[str], *, dry_run: bool
) -> None:
    """Snapshot, admit and link a project, entirely on the internal network.

    The source is mounted read-only into a one-off controller container; only the
    launcher (never a model, never the network) ever sees the archive.
    """
    preflight()
    mounted = validate_import_source(source)
    arguments = [
        "--source", IMPORT_MOUNT, "--project-id", str(project_id),
        "--host", "launcher", "--port", "8765", *options,
    ]  # fmt: skip
    if dry_run:
        arguments.append("--dry-run")
    result = _compose(
        "run", "--rm", "--no-deps", "--no-TTY",
        "--volume", f"{mounted}:{IMPORT_MOUNT}:ro",
        "--volume", f"{IMPORT_SCRIPT}:/opt/import_workspace.py:ro",
        "--entrypoint", "python", "orchestrator", "/opt/import_workspace.py", *arguments,
        capture=True,
    )  # fmt: skip
    print(result.stdout, end="")
    if result.returncode != 0:
        raise StackRefused("the import was refused; review the findings above")
    if dry_run:
        return
    match = HANDLE_LINE.search(result.stdout)
    if match is None:
        raise StackRefused("the import reported no workspace handle")
    _link_workspace(project_id, match.group(1))
    print(f"linked project {project_id} to workspace {match.group(1)}")


def export_reviewed_patches(task_id: str, out_dir: Path) -> list[Path]:
    """Write the task's accepted patches, in apply order, as numbered files."""
    opener = _owner_session()
    url = f"{owner_url()}/api/tasks/{urllib.parse.quote(task_id, safe='')}/review-export"
    with opener.open(url, timeout=30) as response:  # noqa: S310  # nosec B310
        patches = json.load(response)["patches"]
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, item in enumerate(patches, start=1):
        path = out_dir / f"{index:03d}-{item['sha256'][:12]}.patch"
        path.write_text(item["patch"], encoding="utf-8")
        written.append(path)
    return written


def export(task_id: str, out_dir: Path) -> None:
    preflight()
    written = export_reviewed_patches(task_id, out_dir)
    print(f"{len(written)} patch(es) written to {out_dir}; apply in order with `git apply`")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("stage", "up", "status", "login", "check", "stop"):
        commands.add_parser(name)
    import_parser = commands.add_parser("import")
    import_parser.add_argument("project_id", type=UUID)
    import_parser.add_argument("source", type=Path)
    import_parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    import_parser.add_argument("--allow", action="append", default=[], metavar="PATH:RULE")
    import_parser.add_argument("--dry-run", action="store_true")
    export_parser = commands.add_parser("export")
    export_parser.add_argument("task_id")
    export_parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv[1:])
    try:
        if args.command == "export":
            export(args.task_id, args.out_dir)
        elif args.command == "import":
            options = [f"--exclude={glob}" for glob in args.exclude]
            options += [f"--allow={finding}" for finding in args.allow]
            import_project(args.project_id, args.source, options, dry_run=args.dry_run)
        else:
            {
                "stage": stage, "up": up, "status": status,
                "login": login, "check": check, "stop": stop,
            }[args.command]()  # fmt: skip
    except StackRefused as refusal:
        logger.error("%s", refusal)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
