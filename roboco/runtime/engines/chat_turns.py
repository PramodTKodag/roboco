"""Intake and Secretary chats as one bounded model run per human turn.

A live chat is no longer a container holding a login: each human message becomes
one run on the trusted model service. The runner replays the stored transcript in
the prompt (the service keeps no session), guards the input, and turns the run's
reply into the events the panel already consumes. Intake may read the scoped
project's imported workspace through the offline worker; nothing here ever falls
back to another model, provider or login.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from roboco.agent_sdk.intake_driver import extract_draft
from roboco.agent_sdk.prompt_guard import detect_injection, refusal_message
from roboco.agents_config import get_agent_team
from roboco.foundation.identity import AGENTS
from roboco.runtime.engines.subscription_runs import RunLaunch
from roboco.runtime.subscription.contracts import (
    RunResult,
    RunState,
    ToolManifest,
    VerbGrant,
    VerbServer,
    WorkerOperation,
)

if TYPE_CHECKING:
    from uuid import UUID

    from roboco.services.prompter_live import PrompterLiveRegistry

logger = logging.getLogger(__name__)

ChatRole = Literal["intake", "secretary"]

INTAKE_VERBS: Final[tuple[str, ...]] = (
    "search_past_tasks",
    "propose_draft",
    "propose_batch",
)
SECRETARY_VERBS: Final[tuple[str, ...]] = (
    "read_company_state",
    "read_task",
    "search_tasks",
    "submit_directive",
)
_READ_OPERATIONS: Final[tuple[WorkerOperation, ...]] = (
    WorkerOperation.READ_FILE,
    WorkerOperation.LIST_FILES,
    WorkerOperation.SEARCH_TEXT,
)
_VERB_SERVER_FOR_ROLE: Final[dict[ChatRole, VerbServer]] = {
    "intake": VerbServer.INTAKE,
    "secretary": VerbServer.SECRETARY,
}
_VERBS_FOR_ROLE: Final[dict[ChatRole, tuple[str, ...]]] = {
    "intake": INTAKE_VERBS,
    "secretary": SECRETARY_VERBS,
}
_AGENT_SLUG_FOR_ROLE: Final[dict[ChatRole, str]] = {
    "intake": "intake-1",
    "secretary": "secretary-1",
}

# The replayed history is bounded so a long chat can never exceed the prompt limit.
MAX_TRANSCRIPT_BYTES: Final[int] = 256 * 1024

_REFUSAL_FOR_STATE: Final[dict[RunState, str]] = {
    RunState.LOGIN_REQUIRED: (
        "The Claude subscription login is not available. Ask the owner to sign in "
        "again, then retry."
    ),
    RunState.QUOTA_EXHAUSTED: (
        "The Claude subscription usage limit is reached. Retry after it resets."
    ),
    RunState.BLOCKED: "The turn was blocked by the run's safety checks.",
    RunState.CANCELLED: "The turn was cancelled.",
    RunState.FAILED: "The turn failed. Retry the message.",
}


class RunStarter(Protocol):
    async def start(self, launch: RunLaunch) -> str: ...

    async def result(self, agent_slug: str) -> RunResult | None: ...

    async def cancel(self, agent_slug: str) -> None: ...

    def forget(self, agent_slug: str) -> None: ...


class ChatPersistence(Protocol):
    """Durable records a chat keeps; absent for chats with no durable table."""

    async def record_draft(self, session_id: str, draft: dict[str, Any]) -> None: ...

    async def record_reply(self, session_id: str, text: str) -> None: ...


ChatModelResolver = Callable[[ChatRole], Awaitable[str]]


@dataclass(frozen=True)
class ChatScope:
    """What a chat's runs know and may read, fixed when the chat opens."""

    role: ChatRole
    system_prompt: str
    project_id: UUID | None = None
    workspace_handle: UUID | None = None


@dataclass
class _Chat:
    scope: ChatScope
    persistence: ChatPersistence | None
    transcript: list[tuple[str, str]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    turns: set[asyncio.Task[None]] = field(default_factory=set)


def chat_manifest(scope: ChatScope) -> ToolManifest:
    """The tools one chat run may use: its verbs, plus read-only files if linked."""
    has_files = scope.project_id is not None and scope.workspace_handle is not None
    return ToolManifest(
        operations=_READ_OPERATIONS if has_files else (),
        verb_grants=(
            VerbGrant(
                server=_VERB_SERVER_FOR_ROLE[scope.role],
                verbs=_VERBS_FOR_ROLE[scope.role],
            ),
        ),
    )


def compose_turn_prompt(
    system_prompt: str, transcript: list[tuple[str, str]], message: str
) -> str:
    """The system prompt, the bounded replayed history and the new human message."""
    history = _bounded_history(transcript)
    parts = [system_prompt.strip()]
    if history:
        parts.append("## Conversation so far\n\n" + history)
    parts.append("## Latest message from the human\n\n" + message)
    return "\n\n---\n\n".join(parts)


def _bounded_history(transcript: list[tuple[str, str]]) -> str:
    lines = [f"{speaker}: {text}" for speaker, text in transcript]
    while lines and sum(len(line.encode()) for line in lines) > MAX_TRANSCRIPT_BYTES:
        lines.pop(0)
    return "\n\n".join(lines)


class ChatTurnRunner:
    """Runs each human message as one model run and relays the reply."""

    def __init__(
        self,
        supervisor: RunStarter,
        registry: PrompterLiveRegistry,
        resolve_model: ChatModelResolver,
    ) -> None:
        self._supervisor = supervisor
        self._registry = registry
        self._resolve_model = resolve_model
        self._chats: dict[str, _Chat] = {}

    async def open_chat(
        self,
        session_id: str,
        scope: ChatScope,
        persistence: ChatPersistence | None = None,
    ) -> None:
        """Open a chat; a role has one live chat, so an earlier one is closed."""
        for earlier_id, earlier in list(self._chats.items()):
            if earlier.scope.role == scope.role:
                await self.close_chat(earlier_id)
        self._chats[session_id] = _Chat(scope=scope, persistence=persistence)
        logger.info(
            "Chat opened: session=%s role=%s files=%s",
            session_id,
            scope.role,
            scope.workspace_handle is not None,
        )

    def accept(self, session_id: str, text: str) -> bool:
        """Queue one human message as a turn; ``False`` if the chat is unknown."""
        chat = self._chats.get(session_id)
        if chat is None:
            return False
        injection = detect_injection(text)
        if injection is not None:
            logger.warning(
                "Chat turn denied: session=%s reason=%s", session_id, injection
            )
            self._push(session_id, "error", refusal_message(injection))
            return True
        turn = asyncio.create_task(
            self._run_turn(session_id, chat, text), name=f"chat-turn-{session_id[:8]}"
        )
        chat.turns.add(turn)
        turn.add_done_callback(chat.turns.discard)
        return True

    async def close_chat(self, session_id: str) -> None:
        """End the chat: stop any turn in flight and revoke its run credentials."""
        chat = self._chats.pop(session_id, None)
        if chat is None:
            return
        slug = _AGENT_SLUG_FOR_ROLE[chat.scope.role]
        for turn in list(chat.turns):
            turn.cancel()
        await self._supervisor.cancel(slug)
        self._supervisor.forget(slug)
        logger.info("Chat closed: session=%s", session_id)

    async def _run_turn(self, session_id: str, chat: _Chat, text: str) -> None:
        async with chat.lock:
            try:
                result = await self._run_once(session_id, chat, text)
            except Exception:
                logger.exception("Chat turn crashed: session=%s", session_id)
                self._push(session_id, "error", _REFUSAL_FOR_STATE[RunState.FAILED])
                return
        if result.state is not RunState.COMPLETED:
            logger.warning(
                "Chat turn ended: session=%s state=%s reason=%s",
                session_id,
                result.state.value,
                result.reason_code,
            )
            self._push(session_id, "error", _REFUSAL_FOR_STATE[result.state])
            return
        await self._relay_reply(session_id, chat, text, result.text)

    async def _run_once(self, session_id: str, chat: _Chat, text: str) -> RunResult:
        scope = chat.scope
        slug = _AGENT_SLUG_FOR_ROLE[scope.role]
        identity = AGENTS[slug]
        launch = RunLaunch(
            agent_slug=slug,
            agent_id=identity.uuid,
            role=scope.role,
            team=get_agent_team(slug) or "",
            model=await self._resolve_model(scope.role),
            prompt=compose_turn_prompt(scope.system_prompt, chat.transcript, text),
            manifest=chat_manifest(scope),
            project_id=scope.project_id,
            workspace_handle=scope.workspace_handle,
            purpose=scope.role,
            session_id=session_id if scope.role == "intake" else None,
        )
        self._supervisor.forget(slug)
        await self._supervisor.start(launch)
        result = await self._supervisor.result(slug)
        if result is None:
            raise RuntimeError("chat run ended without a result")
        return result

    async def _relay_reply(
        self, session_id: str, chat: _Chat, text: str, reply: str
    ) -> None:
        chat.transcript.append(("Human", text))
        chat.transcript.append(("You", reply))
        self._push(session_id, "text", reply)
        draft = extract_draft(reply) if chat.scope.role == "intake" else None
        if draft is not None:
            await self._record_and_push_draft(session_id, chat, draft)
        await self._record_reply(session_id, chat, reply)
        self._push(session_id, "turn_end", "")

    async def _record_and_push_draft(
        self, session_id: str, chat: _Chat, draft: dict[str, Any]
    ) -> None:
        if chat.persistence is not None:
            try:
                await chat.persistence.record_draft(session_id, draft)
            except Exception:
                logger.exception("Draft not persisted: session=%s", session_id)
        self._registry.push(
            session_id, {"kind": "draft", "text": "", "tool": "", "data": draft}
        )

    async def _record_reply(self, session_id: str, chat: _Chat, reply: str) -> None:
        if chat.persistence is None or not reply.strip():
            return
        try:
            await chat.persistence.record_reply(session_id, reply)
        except Exception:
            logger.exception("Reply not persisted: session=%s", session_id)

    def _push(self, session_id: str, kind: str, text: str) -> None:
        self._registry.push(
            session_id, {"kind": kind, "text": text, "tool": "", "data": {}}
        )

