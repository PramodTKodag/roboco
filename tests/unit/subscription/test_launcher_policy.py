"""Launcher policy: every rule must hold server-side, even for a client that skips
validation. Pure tests; real Docker behavior is in tests/integration."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from roboco.runtime.subscription.contracts import WorkerJob, WorkerOperation
from roboco.runtime.subscription.launcher_policy import (
    ClientAuthenticator,
    JobRejection,
    LauncherConfig,
    LauncherRegistry,
    build_worker_run_argv,
)
from roboco.runtime.subscription.launcher_protocol import (
    MAX_FRAME_BYTES,
    LauncherAction,
    ProtocolError,
    parse_frame,
)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
ADMIN, EXECUTOR = (
    "admin-token-synthetic-0123456789",
    "executor-token-synthetic-0123456789",
)
CONFIG = LauncherConfig(image="sha256:" + "a" * 64)


def _registry() -> LauncherRegistry:
    return LauncherRegistry(run_ttl=timedelta(minutes=30))


def _registered(
    registry: LauncherRegistry,
) -> tuple[UUID, UUID, UUID]:
    project, workspace, run = uuid4(), uuid4(), uuid4()
    registry.register_workspace(workspace, project)
    registry.register_run(run, project, workspace, NOW)
    return project, workspace, run


def _job(project: UUID, workspace: UUID, run: UUID, **overrides: object) -> WorkerJob:
    base: dict[str, object] = {
        "job_id": uuid4(),
        "run_id": run,
        "project_id": project,
        "workspace_handle": workspace,
        "operation": WorkerOperation.RUN_COMMAND,
        "payload": {"operation": "run_command", "command": "true"},
    }
    return WorkerJob(**{**base, **overrides})  # type: ignore[arg-type]


def test_registered_job_is_authorized_and_mount_is_derived_from_the_handle() -> None:
    registry = _registry()
    project, workspace, run = _registered(registry)
    authorized = registry.authorize(_job(project, workspace, run), NOW)
    assert authorized.volume_name == f"roboco_ws_{workspace.hex}"
    assert authorized.container_name.startswith("roboco-worker-")


def test_unregistered_run_is_rejected() -> None:
    registry = _registry()
    project, workspace, _ = _registered(registry)
    with pytest.raises(JobRejection) as caught:
        registry.authorize(_job(project, workspace, uuid4()), NOW)
    assert caught.value.reason_code == "run_unknown"


def test_unknown_workspace_is_rejected() -> None:
    registry = _registry()
    project, _, run = _registered(registry)
    with pytest.raises(JobRejection) as caught:
        registry.authorize(_job(project, uuid4(), run), NOW)
    assert caught.value.reason_code == "workspace_mismatch"


def test_other_projects_workspace_is_rejected() -> None:
    registry = _registry()
    _, workspace, run = _registered(registry)
    with pytest.raises(JobRejection) as caught:
        registry.authorize(_job(uuid4(), workspace, run), NOW)
    assert caught.value.reason_code == "project_mismatch"


def test_a_run_cannot_use_another_runs_workspace() -> None:
    registry = _registry()
    project, _, run = _registered(registry)
    other_workspace = uuid4()
    registry.register_workspace(other_workspace, project)
    with pytest.raises(JobRejection) as caught:
        registry.authorize(_job(project, other_workspace, run), NOW)
    assert caught.value.reason_code == "workspace_mismatch"


def test_expired_run_is_rejected() -> None:
    registry = _registry()
    project, workspace, run = _registered(registry)
    with pytest.raises(JobRejection) as caught:
        registry.authorize(_job(project, workspace, run), NOW + timedelta(minutes=31))
    assert caught.value.reason_code == "run_expired"


def test_cancelled_run_is_rejected_and_stays_cancelled() -> None:
    registry = _registry()
    project, workspace, run = _registered(registry)
    registry.cancel_run(run)
    for _ in range(2):
        with pytest.raises(JobRejection) as caught:
            registry.authorize(_job(project, workspace, run), NOW)
        assert caught.value.reason_code == "run_cancelled"


def test_replayed_job_id_is_rejected() -> None:
    registry = _registry()
    project, workspace, run = _registered(registry)
    job = _job(project, workspace, run)
    registry.authorize(job, NOW)
    with pytest.raises(JobRejection) as caught:
        registry.authorize(job, NOW)
    assert caught.value.reason_code == "job_replayed"


def test_run_registration_cannot_be_rebound() -> None:
    registry = _registry()
    project, _, run = _registered(registry)
    other_workspace = uuid4()
    registry.register_workspace(other_workspace, project)
    with pytest.raises(JobRejection) as caught:
        registry.register_run(run, project, other_workspace, NOW)
    assert caught.value.reason_code == "run_already_registered"


def test_run_cannot_register_an_unregistered_workspace() -> None:
    registry = _registry()
    with pytest.raises(JobRejection) as caught:
        registry.register_run(uuid4(), uuid4(), uuid4(), NOW)
    assert caught.value.reason_code == "workspace_unknown"


def test_workspace_cannot_move_between_projects() -> None:
    registry = _registry()
    project, workspace, _ = _registered(registry)
    registry.register_workspace(workspace, project)  # idempotent for same owner
    with pytest.raises(JobRejection):
        registry.register_workspace(workspace, uuid4())


def test_pending_run_limit_is_enforced() -> None:
    registry = LauncherRegistry(run_ttl=timedelta(minutes=30), max_runs=2)
    project, workspace = uuid4(), uuid4()
    registry.register_workspace(workspace, project)
    registry.register_run(uuid4(), project, workspace, NOW)
    registry.register_run(uuid4(), project, workspace, NOW)
    with pytest.raises(JobRejection) as caught:
        registry.register_run(uuid4(), project, workspace, NOW)
    assert caught.value.reason_code == "too_many_runs"


def test_expired_runs_are_purged_so_the_limit_recovers() -> None:
    registry = LauncherRegistry(run_ttl=timedelta(minutes=30), max_runs=1)
    project, workspace = uuid4(), uuid4()
    registry.register_workspace(workspace, project)
    registry.register_run(uuid4(), project, workspace, NOW)
    registry.register_run(uuid4(), project, workspace, NOW + timedelta(minutes=31))


# --- docker argv -----------------------------------------------------------


def _argv() -> list[str]:
    registry = _registry()
    project, workspace, run = _registered(registry)
    authorized = registry.authorize(_job(project, workspace, run), NOW)
    return build_worker_run_argv(CONFIG, authorized)


def test_argv_applies_the_fixed_security_profile() -> None:
    argv = _argv()
    joined = " ".join(argv)
    for expected in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 256",
        "--memory 2g",
        "--cpus 2",
        "--user 10001:10001",
        "--ipc none",
    ):
        assert expected in joined
    assert argv[argv.index(CONFIG.image) - 1] != "-e"


def test_argv_never_carries_forbidden_launch_options() -> None:
    joined = " ".join(_argv())
    for forbidden in (
        "--privileged",
        "--device",
        "docker.sock",
        "--network host",
        "--pid ",
        "--pid=",
        "--cap-add",
        "-v ",
        "--volumes-from",
        "--env",
        " -e ",
        "--add-host",
        "--security-opt seccomp=unconfined",
    ):
        assert forbidden not in joined


def test_argv_has_exactly_one_mount_and_it_is_the_derived_volume() -> None:
    argv = _argv()
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    assert len(mounts) == 1
    assert mounts[0].startswith("type=volume,src=roboco_ws_")
    assert mounts[0].endswith(",dst=/workspace")


def test_argv_labels_identify_owned_containers() -> None:
    argv = _argv()
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert "roboco.personal.worker=1" in labels
    assert any(label.startswith("roboco.personal.run=") for label in labels)


# --- protocol / auth -------------------------------------------------------


def _frame(**fields: object) -> bytes:
    return json.dumps(fields).encode() + b"\n"


def test_execute_frame_with_launch_shaping_fields_is_rejected_server_side() -> None:
    job = _job(*_registered(_registry()))
    raw = job.model_dump(mode="json")
    raw["payload"]["image"] = "evil"
    with pytest.raises(ProtocolError):
        parse_frame(_frame(token=EXECUTOR, action="execute", body={"job": raw}))
    raw2 = job.model_dump(mode="json")
    with pytest.raises(ProtocolError):
        parse_frame(
            _frame(
                token=EXECUTOR,
                action="execute",
                body={"job": raw2, "host_path": "/etc"},
            )
        )


def test_unknown_action_and_malformed_frames_are_rejected() -> None:
    for raw in (
        _frame(token=ADMIN, action="docker_run", body={}),
        b"not json\n",
        b"[]\n",
        _frame(action="health", body={}),
        _frame(token=ADMIN, action="health", body={}, extra=1),
    ):
        with pytest.raises(ProtocolError):
            parse_frame(raw)


def test_oversized_frame_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        parse_frame(b"x" * (MAX_FRAME_BYTES + 1))


def test_valid_execute_frame_round_trips() -> None:
    job = _job(*_registered(_registry()))
    frame = parse_frame(
        _frame(
            token=EXECUTOR, action="execute", body={"job": job.model_dump(mode="json")}
        )
    )
    assert frame.action is LauncherAction.EXECUTE
    assert frame.body.job == job


def test_tokens_grant_only_their_own_actions() -> None:
    auth = ClientAuthenticator(admin_token=ADMIN, executor_token=EXECUTOR)
    assert auth.allows(ADMIN, LauncherAction.REGISTER_RUN)
    assert auth.allows(ADMIN, LauncherAction.CANCEL_RUN)
    assert not auth.allows(ADMIN, LauncherAction.EXECUTE)
    assert auth.allows(EXECUTOR, LauncherAction.EXECUTE)
    assert not auth.allows(EXECUTOR, LauncherAction.REGISTER_RUN)
    assert not auth.allows(EXECUTOR, LauncherAction.CANCEL_RUN)
    assert not auth.allows("wrong", LauncherAction.HEALTH)
    assert not auth.allows("", LauncherAction.HEALTH)


def test_authenticator_rejects_weak_or_identical_tokens() -> None:
    with pytest.raises(ValueError, match="distinct"):
        ClientAuthenticator(admin_token=ADMIN, executor_token=ADMIN)
    with pytest.raises(ValueError, match="at least"):
        ClientAuthenticator(admin_token="short", executor_token=EXECUTOR)
