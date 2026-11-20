"""Live Intake and Secretary chats, run as one model run per human turn.

A chat has no container: ``start_*_session`` opens the relay, builds the chat's
fixed scope (system prompt, and for a single linked project its read-only
workspace) and hands every human message to the ``ChatTurnRunner``. The usage-dir
helpers below are the delivery providers' and stay here with the rest of the
orchestrator's engines.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from roboco.runtime.engines.chat_turns import ChatScope, ChatTurnRunner
from roboco.runtime.engines.subscription_runs import UnroutableModel
from roboco.runtime.orchestrator import (
    INTAKE_AGENT_ID,
    NEBIUS_USAGE_DATA_DIR,
    PROJECT_HOST_PATH,
    SECRETARY_AGENT_ID,
    logger,
)

if TYPE_CHECKING:
    # Also bound into this module's real (non-TYPE_CHECKING) globals at
    # runtime by orchestrator.py, right after the class is defined -- see
    # its bottom-of-file comment.
    from roboco.runtime.orchestrator import (
        AgentOrchestrator,
    )


if TYPE_CHECKING:
    from roboco.runtime.engines._types import AgentOrchestratorSelf as _Base
else:
    _Base = object

_INTAKE_FILES_AMBIENT = (
    "## Repository access\n\n"
    "You may read the scoped project's files with the worker tools read_file, "
    "list_files and search_text. Draft against the real tree, not from memory. "
    "You cannot change anything."
)
_INTAKE_NO_FILES_AMBIENT = (
    "## Repository access\n\n"
    "This chat has no readable repository: either several projects are in scope "
    "or the project has no imported workspace. Draft from the conversation and "
    "past tasks only, and say so where the spec depends on code you cannot see."
)


class _LivePersistence:
    """Durable intake records: proposed drafts and the assistant's replies."""

    async def record_draft(self, session_id: str, draft: dict[str, Any]) -> None:
        from roboco.db.base import get_db_context
        from roboco.services.prompter import get_prompter_service

        async with get_db_context() as db:
            await get_prompter_service(db).record_live_draft(session_id, draft)

    async def record_reply(self, session_id: str, text: str) -> None:
        from roboco.db.base import get_db_context
        from roboco.services.prompter import get_prompter_service

        async with get_db_context() as db:
            await get_prompter_service(db).record_live_message(
                session_id, "assistant", text
            )


class InteractiveSessionsEngine(_Base):
    """Mixin holding the "interactive_sessions" methods moved out of
    AgentOrchestrator."""

    # Re-declared on the concrete class for the same reason as
    # ``SpawnLaunchEngine._provider_registry``: a member that one method both
    # reads and assigns needs a bare declaration here for mypy.
    if TYPE_CHECKING:
        _chat_runner: ChatTurnRunner | None

    @staticmethod
    def _grok_usage_dir(agent_id: str) -> Path:
        """Per-agent grok usage dir under :meth:`_grok_usage_root`.

        Single source of truth for BOTH the pre-create/mount side
        (``_ensure_grok_usage_dir``) and the finalize read side
        (``_grok_usage_json``) so they can never drift. ``agent_id`` is validated
        as a single safe path segment first — ``_safe_agent_path_segment`` rejects
        ``.`` / ``..`` / separators / NUL so a bad id raises rather than silently
        remapping or traversing. The read side additionally reduces the id to its
        final path component (``os.path.basename``) — the CodeQL-recognized
        path-injection barrier.
        """
        return AgentOrchestrator._grok_usage_root() / (
            AgentOrchestrator._safe_agent_path_segment(agent_id)
        )

    def _ensure_grok_usage_dir(self, agent_id: str) -> None:
        """Pre-create the agent's grok usage dir (world-writable) before the mount.

        On Linux, ``docker run -v`` auto-creates a MISSING bind source as
        ``root:root``, so the non-root ``agent`` user EACCESes when the grok
        entrypoint / interactive driver writes ``usage.json`` there. Creating the
        dir ``0777`` first makes the mounted dir writable regardless of the agent
        uid; the orchestrator (root) can still read it back at finalize.
        """
        target = self._grok_usage_dir(agent_id)
        try:
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o777)
        except OSError as exc:
            logger.warning(
                "could not pre-create grok usage dir; grok agent may EACCES",
                agent_id=agent_id,
                path=str(target),
                error=str(exc),
            )

    @staticmethod
    def _codex_usage_dir(agent_id: str) -> Path:
        """Per-agent codex usage dir under :meth:`_codex_usage_root`.

        Single source of truth for BOTH the pre-create/mount side
        (``_ensure_codex_usage_dir``) and the finalize read side
        (``_codex_usage_json``), mirroring ``_grok_usage_dir``.
        """
        return AgentOrchestrator._codex_usage_root() / (
            AgentOrchestrator._safe_agent_path_segment(agent_id)
        )

    def _ensure_codex_usage_dir(self, agent_id: str) -> None:
        """Pre-create the agent's codex usage dir (world-writable) before the mount.

        Same EACCES concern as ``_ensure_grok_usage_dir``: a missing bind
        source is auto-created ``root:root`` on Linux, which the non-root
        ``agent`` user can't write into.
        """
        target = self._codex_usage_dir(agent_id)
        try:
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o777)
        except OSError as exc:
            logger.warning(
                "could not pre-create codex usage dir; codex agent may EACCES",
                agent_id=agent_id,
                path=str(target),
                error=str(exc),
            )

    @staticmethod
    def _gemini_usage_dir(agent_id: str) -> Path:
        """Per-agent gemini usage dir under :meth:`_gemini_usage_root`.

        Single source of truth for BOTH the pre-create/mount side
        (``_ensure_gemini_usage_dir``) and the finalize read side
        (``_gemini_usage_json``) — see :meth:`_grok_usage_dir`'s docstring for
        the ``_safe_agent_path_segment`` traversal-rejection rationale, shared
        verbatim here.
        """
        return AgentOrchestrator._gemini_usage_root() / (
            AgentOrchestrator._safe_agent_path_segment(agent_id)
        )

    def _ensure_gemini_usage_dir(self, agent_id: str) -> None:
        """Pre-create the agent's gemini usage dir (world-writable) before the mount.

        Same EACCES rationale as :meth:`_ensure_grok_usage_dir`: a missing
        Linux bind-mount source is auto-created ``root:root``, so the non-root
        ``agent`` user would fail to write ``usage.json`` there without this.
        """
        target = self._gemini_usage_dir(agent_id)
        try:
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o777)
        except OSError as exc:
            logger.warning(
                "could not pre-create gemini usage dir; gemini agent may EACCES",
                agent_id=agent_id,
                path=str(target),
                error=str(exc),
            )

    @staticmethod
    def _kimi_usage_dir(agent_id: str) -> Path:
        """Per-agent kimi usage dir under :meth:`_kimi_usage_root`.

        Single source of truth for BOTH the pre-create/mount side
        (``_ensure_kimi_usage_dir``) and the finalize read side
        (``_kimi_usage_json``), mirroring ``_grok_usage_dir``.
        """
        return AgentOrchestrator._kimi_usage_root() / (
            AgentOrchestrator._safe_agent_path_segment(agent_id)
        )

    def _ensure_kimi_usage_dir(self, agent_id: str) -> None:
        """Pre-create the agent's kimi usage dir (world-writable) before the mount.

        Same EACCES concern as ``_ensure_grok_usage_dir``: a missing bind
        source is auto-created ``root:root`` on Linux, which the non-root
        ``agent`` user can't write into.
        """
        target = self._kimi_usage_dir(agent_id)
        try:
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o777)
        except OSError as exc:
            logger.warning(
                "could not pre-create kimi usage dir; kimi agent may EACCES",
                agent_id=agent_id,
                path=str(target),
                error=str(exc),
            )

    @staticmethod
    def _nebius_usage_root() -> Path:
        """The base dir all per-agent nebius usage dirs live under (no agent id).

        Same compose-vs-local branch as :meth:`_kimi_usage_root`.
        """
        if PROJECT_HOST_PATH:
            return Path(NEBIUS_USAGE_DATA_DIR)
        return Path(tempfile.gettempdir()) / "roboco-nebius-usage"

    @staticmethod
    def _nebius_usage_dir(agent_id: str) -> Path:
        """Per-agent nebius usage dir under :meth:`_nebius_usage_root`.

        Single source of truth for BOTH the pre-create/mount side
        (``_ensure_nebius_usage_dir``) and the finalize read side
        (``_nebius_usage_json``), mirroring ``_kimi_usage_dir``.
        """
        return AgentOrchestrator._nebius_usage_root() / (
            AgentOrchestrator._safe_agent_path_segment(agent_id)
        )

    def _ensure_nebius_usage_dir(self, agent_id: str) -> None:
        """Pre-create the agent's nebius usage dir before the mount.

        Same EACCES concern as ``_ensure_kimi_usage_dir``: a missing bind
        source is auto-created ``root:root`` on Linux, which the non-root
        ``agent`` user can't write into.
        """
        target = self._nebius_usage_dir(agent_id)
        try:
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o777)
        except OSError as exc:
            logger.warning(
                "could not pre-create nebius usage dir; agent may EACCES",
                agent_id=agent_id,
                path=str(target),
                error=str(exc),
            )

    async def _resolve_history_digest_ambient(
        self,
        project_slug: str | None,
        product_id: str | None = None,
        project_ids: list[str] | None = None,
    ) -> str | None:
        """Resolve the prompter's task-history-digest ambient block for this scope.

        Covers all three intake scopes including ``project_ids`` (a MegaTask) —
        the digest is meant to span every project the intake agent is reading.
        Best-effort: returns None on any failure or empty scope so history
        resolution can never block a spawn.
        """
        try:
            from roboco.db.base import get_session_factory
            from roboco.services.prompter import history_digest_layer

            factory = get_session_factory()
            async with factory() as db:
                projects = await self._resolve_history_digest_projects(
                    db,
                    project_slug=project_slug,
                    product_id=product_id,
                    project_ids=project_ids,
                )
                return await history_digest_layer(db, projects)
        except Exception as exc:
            logger.warning(
                "History digest ambient resolution failed (non-fatal)",
                project_slug=project_slug,
                error=str(exc),
            )
            return None

    @staticmethod
    async def _resolve_history_digest_projects(
        db: Any,
        *,
        project_slug: str | None,
        product_id: str | None,
        project_ids: list[str] | None,
    ) -> list[Any]:
        """The in-scope ProjectTable rows for the history digest — single repo,
        product (all cell projects), or an explicit MegaTask project_ids set."""
        if project_ids:
            return await AgentOrchestrator._projects_by_ids(db, project_ids)
        if product_id is not None:
            return await AgentOrchestrator._ambient_product_projects(db, product_id)
        if project_slug:
            from roboco.services.project import get_project_service

            project = await get_project_service(db).get_by_slug(project_slug)
            return [project] if project is not None else []
        return []

    async def _resolve_intake_ambient(
        self,
        project_slug: str | None,
        *,
        product_id: str | None,
        project_ids: list[str] | None,
    ) -> str | None:
        """The intake spawn's full ambient block: conventions + history digest,
        joined with ``compose_prompt``'s own layer separator."""
        conventions_ambient = await self._resolve_conventions_ambient(
            project_slug, product_id=product_id, project_ids=project_ids
        )
        history_ambient = await self._resolve_history_digest_ambient(
            project_slug, product_id=product_id, project_ids=project_ids
        )
        return (
            "\n\n---\n\n".join(
                part
                for part in (
                    conventions_ambient,
                    history_ambient,
                )
                if part
            )
            or None
        )

    @staticmethod
    def _require_one_intake_scope(
        project_slug: str | None,
        product_id: str | None,
        project_ids: list[str] | None,
    ) -> None:
        """Exactly one intake scope: a single project, a product, or a MegaTask's
        explicit project set."""
        chosen = sum(1 for scope in (project_slug, product_id, project_ids) if scope)
        if chosen != 1:
            raise ValueError(
                "intake scope requires exactly one of project_slug / product_id"
                " / project_ids"
            )

    async def _persist_intake_first_message(self, session_id: str, text: str) -> None:
        """Durably record a live intake session's opening human turn.

        Best-effort (mirrors ``_inject_board_brief_into_parked_intake``): a
        persistence failure is logged, never raised into the delivery path.
        """
        from roboco.db.base import get_db_context
        from roboco.services.prompter import get_prompter_service

        try:
            async with get_db_context() as db:
                await get_prompter_service(db).record_live_message(
                    session_id, "user", text
                )
        except Exception as exc:
            logger.warning(
                "Failed to persist intake opening message",
                session_id=session_id,
                error=str(exc),
            )

    def _chat_turn_runner(self) -> ChatTurnRunner:
        """The chat turn runner, built once on the shared run supervisor."""
        from roboco.services.prompter_live import get_live_registry

        if self._chat_runner is None:
            registry = get_live_registry()
            runner = ChatTurnRunner(
                self._ensure_run_supervisor(), registry, self._resolve_chat_model
            )
            registry.set_turn_handler(runner.accept)
            self._chat_runner = runner
        return self._chat_runner

    async def _resolve_chat_model(self, role: str) -> str:
        """The model a chat runs on: its routed Anthropic model, else refused."""
        from roboco.models.base import ModelProvider

        agent_id = INTAKE_AGENT_ID if role == "intake" else SECRETARY_AGENT_ID
        route = await self._resolve_agent_route(agent_id)
        if route.provider_type != ModelProvider.ANTHROPIC:
            raise UnroutableModel(f"{agent_id}:{route.provider_type.value}")
        return str(route.model_name)

    async def _intake_project_binding(
        self,
        project_slug: str | None,
        product_id: str | None,
        project_ids: list[str] | None,
    ) -> tuple[UUID | None, UUID | None]:
        """``(project_id, workspace_handle)`` when the scope is exactly one project.

        A product or multi-project scope has no single workspace to read, so its
        chat gets no file tools.
        """
        from roboco.db.base import get_session_factory

        async with get_session_factory()() as db:
            projects = await self._resolve_history_digest_projects(
                db,
                project_slug=project_slug,
                product_id=product_id,
                project_ids=project_ids,
            )
        if product_id is not None or len(projects) != 1:
            return None, None
        project = projects[0]
        return UUID(str(project.id)), project.offline_workspace_handle

    async def _build_intake_scope(
        self,
        project_slug: str | None,
        product_id: str | None,
        project_ids: list[str] | None,
    ) -> ChatScope:
        project_id, workspace_handle = await self._intake_project_binding(
            project_slug, product_id, project_ids
        )
        files_ambient = (
            _INTAKE_FILES_AMBIENT
            if workspace_handle is not None
            else _INTAKE_NO_FILES_AMBIENT
        )
        scope_ambient = (
            "## Scoped project\n\n"
            f"Use project id {project_id} as the_work[].project_id."
            if project_id is not None
            else ""
        )
        ambient = await self._resolve_intake_ambient(
            project_slug, product_id=product_id, project_ids=project_ids
        )
        layers = [files_ambient, scope_ambient, ambient]
        prompt_path = self._generate_composed_prompt(
            INTAKE_AGENT_ID,
            ambient="\n\n---\n\n".join(layer for layer in layers if layer),
        )
        return ChatScope(
            role="intake",
            system_prompt=prompt_path.read_text(),
            project_id=project_id if workspace_handle is not None else None,
            workspace_handle=workspace_handle,
        )

    async def start_intake_session(
        self,
        session_id: str,
        *,
        project_slug: str | None = None,
        product_id: str | None = None,
        project_ids: list[str] | None = None,
        initial_message: str | None = None,
    ) -> None:
        """Open an intake chat; every human message then runs as one model run.

        Exactly one of ``project_slug`` / ``product_id`` / ``project_ids`` (a
        MegaTask) must be given. A failure is pushed onto the relay as an
        ``error`` event and closes the session, so the panel shows it.
        """
        from roboco.services.prompter_live import get_live_registry

        self._require_one_intake_scope(project_slug, product_id, project_ids)
        registry = get_live_registry()
        registry.close_by_agent(INTAKE_AGENT_ID)
        registry.open(session_id, INTAKE_AGENT_ID)
        try:
            scope = await self._build_intake_scope(
                project_slug, product_id, project_ids
            )
            await self._chat_turn_runner().open_chat(
                session_id, scope, _LivePersistence()
            )
        except Exception as exc:
            logger.error(
                "Intake chat could not start", session_id=session_id, error=str(exc)
            )
            registry.push(
                session_id,
                {"kind": "error", "text": f"Couldn't start the intake agent: {exc}"},
            )
            registry.close(session_id)
            return
        logger.info(
            "Intake chat opened",
            session_id=session_id,
            files=scope.workspace_handle is not None,
        )
        if initial_message:
            await self._persist_intake_first_message(session_id, initial_message)
            await registry.deliver(session_id, initial_message)

    async def start_secretary_session(
        self, session_id: str, *, initial_message: str | None = None
    ) -> None:
        """Open a Secretary chat; every human message then runs as one model run."""
        from roboco.services.prompter_live import get_live_registry

        registry = get_live_registry()
        registry.close_by_agent(SECRETARY_AGENT_ID)
        registry.open(session_id, SECRETARY_AGENT_ID)
        try:
            prompt_path = self._generate_composed_prompt(SECRETARY_AGENT_ID)
            scope = ChatScope(role="secretary", system_prompt=prompt_path.read_text())
            await self._chat_turn_runner().open_chat(session_id, scope)
        except Exception as exc:
            logger.error(
                "Secretary chat could not start", session_id=session_id, error=str(exc)
            )
            registry.push(
                session_id,
                {"kind": "error", "text": f"Couldn't start the Secretary: {exc}"},
            )
            registry.close(session_id)
            return
        logger.info("Secretary chat opened", session_id=session_id)
        if initial_message:
            await registry.deliver(session_id, initial_message)
