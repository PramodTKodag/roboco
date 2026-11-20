"""
Message Extraction Service

Extracts structured messages from raw agent LLM output.
Uses pattern matching and optional LLM-based classification to
identify different message types: reasoning, dialogue, decisions,
actions, blockers, and technical content.

Flow:
1. TranscriptionService yields ready buffer
2. ExtractionService analyzes content
3. Produces list of ExtractedMessage objects
4. Messages are stored and broadcast
"""

import re
from typing import Any
from uuid import UUID, uuid4

import structlog

from roboco.models import MessageType
from roboco.models.extraction import (
    ExtractionConfig,
    ExtractionContext,
    ExtractionResult,
)
from roboco.models.message import ExtractedMessage

logger = structlog.get_logger()

# Maximum length for raw excerpt storage
MAX_EXCERPT_LENGTH = 200


# =============================================================================
# EXTRACTION PATTERNS
# =============================================================================

# Patterns for identifying message types
# These are heuristics; can be enhanced with LLM classification

REASONING_PATTERNS = [
    r"(?i)^I(?:'m| am) thinking",
    r"(?i)^Let me (?:think|consider|analyze)",
    r"(?i)^Hmm,? ",
    r"(?i)^I need to",
    r"(?i)^First,? I(?:'ll| will| should)",
    r"(?i)^My approach",
    r"(?i)^To solve this",
    r"(?i)^The (?:issue|problem|question) (?:is|seems)",
    r"(?i)^Looking at",
    r"(?i)^Analyzing",
    r"(?i)^Considering",
]

DIALOGUE_PATTERNS = [
    r"(?i)^Hey,? ",
    r"(?i)^Hi,? ",
    r"(?i)^@\w+",  # Mentions
    r"(?i)^Can (?:you|someone)",
    r"(?i)^Could (?:you|someone)",
    r"(?i)^Would (?:you|someone)",
    r"(?i)^I(?:'m| am) asking",
    r"(?i)^Question:",
    r"(?i)^Does anyone",
    r"(?i)^What do you think",
    r"(?i)^Thoughts\?",
    r"\?$",  # Ends with question mark
]

DECISION_PATTERNS = [
    r"(?i)^I(?:'ve| have) decided",
    r"(?i)^Decision:",
    r"(?i)^I(?:'ll| will) go with",
    r"(?i)^Let(?:'s| us) use",
    r"(?i)^We(?:'ll| will) use",
    r"(?i)^The (?:approach|solution|answer) is",
    r"(?i)^Choosing",
    r"(?i)^Selected:",
    r"(?i)^Going with",
    r"(?i)^After consideration,? I(?:'ll| will)",
]

ACTION_PATTERNS = [
    r"(?i)^Starting",
    r"(?i)^Creating",
    r"(?i)^Writing",
    r"(?i)^Implementing",
    r"(?i)^Running",
    r"(?i)^Executing",
    r"(?i)^Testing",
    r"(?i)^Committing",
    r"(?i)^Pushing",
    r"(?i)^Deploying",
    r"(?i)^Task (?:complete|done|finished)",
    r"(?i)^Done:",
    r"(?i)^Completed:",
    r"(?i)^✓",
    r"(?i)^✅",
]

BLOCKER_PATTERNS = [
    r"(?i)^Blocked:",
    r"(?i)^Blocker:",
    r"(?i)^I(?:'m| am) blocked",
    r"(?i)^Cannot proceed",
    r"(?i)^Waiting (?:on|for)",
    r"(?i)^Need (?:help|assistance|input)",
    r"(?i)^Stuck on",
    r"(?i)^Dependency:",
    r"(?i)^Missing:",
    r"(?i)^Error:",
    r"(?i)^Failed:",
    r"(?i)^Unable to",
    r"(?i)^🚫",
    r"(?i)^⛔",
]

TECHNICAL_PATTERNS = [
    r"```",  # Code blocks
    r"(?i)^The (?:function|class|method|variable)",
    r"(?i)^This (?:code|implementation|function)",
    r"(?i)^Here(?:'s| is) (?:the|how)",
    r"(?i)^API:",
    r"(?i)^Schema:",
    r"(?i)^Endpoint:",
    r"(?i)^Response:",
    r"(?i)^Request:",
    r"^[A-Z][a-zA-Z]+(?:Error|Exception)",  # Exception names
]


class ExtractionService:
    """
    Service for extracting structured messages from raw agent output.

    Uses pattern matching to classify segments into message types.
    Can be extended with LLM-based classification for better accuracy.

    Usage:
        service = ExtractionService()
        result = await service.extract(buffer)
        for message in result.messages:
            await store_message(message)
    """

    @staticmethod
    def _compile_patterns() -> dict[MessageType, list[re.Pattern]]:
        """Pre-compile the per-message-type regex pattern lists."""
        return {
            MessageType.REASONING: [re.compile(p) for p in REASONING_PATTERNS],
            MessageType.DIALOGUE: [re.compile(p) for p in DIALOGUE_PATTERNS],
            MessageType.DECISION: [re.compile(p) for p in DECISION_PATTERNS],
            MessageType.ACTION: [re.compile(p) for p in ACTION_PATTERNS],
            MessageType.BLOCKER: [re.compile(p) for p in BLOCKER_PATTERNS],
            MessageType.TECHNICAL: [re.compile(p) for p in TECHNICAL_PATTERNS],
        }

    def __init__(self, config: ExtractionConfig | None = None) -> None:
        self.config = config or ExtractionConfig()
        self.log = logger.bind(component="extraction")
        self._compiled_patterns = self._compile_patterns()
        self._mention_pattern = re.compile(r"@(\w+)")

    async def extract(
        self, ctx: ExtractionContext, *, _pilot_verdicts: list | None = None
    ) -> ExtractionResult:
        """
        Extract messages from raw content.

        Args:
            ctx: Extraction context with content and metadata
            _pilot_verdicts: internal B10 seam - precomputed confident
                Decisions verdicts (from ``extract_with_llm``, which must
                not trigger a second Decisions call). None = compute here.

        Returns:
            ExtractionResult with extracted messages
        """
        if len(ctx.content) < self.config.min_content_length:
            return ExtractionResult(
                messages=[],
                raw_content=ctx.content,
                agent_id=ctx.agent_id,
                channel_id=ctx.channel_id,
                session_id=ctx.session_id,
            )

        # Segment the content
        segments = self._segment_content(ctx.content)

        messages: list[ExtractedMessage] = []
        pattern_matches: dict[str, list[str]] = {}
        confidence_scores: dict[UUID, float] = {}

        capped_segments = segments[: self.config.max_segments_per_buffer]
        if _pilot_verdicts is None:
            # B10 segment_classify: one batched Decisions classification
            # for the whole buffer; None entries (and an empty list on any
            # failure/off) keep the regex result per segment.
            _pilot_verdicts = await self._decisions_segment_types(
                [s for s in capped_segments if s.strip()]
            )
        pilot_verdicts = _pilot_verdicts
        pilot_idx = 0

        for segment in capped_segments:
            if not segment.strip():
                continue

            # Classify segment
            msg_type, confidence, matches = self._classify_segment(segment)
            verdict = (
                pilot_verdicts[pilot_idx] if pilot_idx < len(pilot_verdicts) else None
            )
            pilot_idx += 1
            msg_type, confidence = self._resolved_segment_type(
                verdict, msg_type, confidence
            )

            # Store pattern matches for debugging
            if matches:
                pattern_matches[segment[:50]] = matches

            # Create message
            message = self._build_segment_message(ctx, segment, msg_type, confidence)

            messages.append(message)
            confidence_scores[message.id] = confidence

        result = ExtractionResult(
            messages=messages,
            raw_content=ctx.content,
            agent_id=ctx.agent_id,
            channel_id=ctx.channel_id,
            session_id=ctx.session_id,
            pattern_matches=pattern_matches,
            confidence_scores=confidence_scores,
        )

        self.log.info(
            "Extraction complete",
            agent_id=str(ctx.agent_id),
            message_count=result.message_count,
            types=result.types_extracted,
        )

        return result

    def _resolved_segment_type(
        self,
        verdict: tuple[str, float] | None,
        msg_type: MessageType,
        confidence: float,
    ) -> tuple[MessageType, float]:
        """Apply one confident Decisions pilot verdict on top of the regex
        classification. A confident verdict REPLACES the regex result (and
        lets the caller avoid the expensive full-LLM fallback); an unknown
        type string keeps the regex classification unchanged."""
        if verdict is None:
            return msg_type, confidence
        pilot_type, pilot_confidence = verdict
        try:
            resolved = MessageType(pilot_type)
        except ValueError:
            return msg_type, confidence
        return resolved, pilot_confidence

    def _collect_mentions(self, segment: str) -> list[UUID]:
        """Extract mention names when configured.

        In production, resolve names to agent UUIDs
        For now, just log them
        """
        mentions: list[UUID] = []
        if not self.config.extract_mentions:
            return mentions
        mention_names = self._mention_pattern.findall(segment)
        if mention_names:
            self.log.debug("Found mentions", mentions=mention_names)
        return mentions

    def _build_segment_message(
        self,
        ctx: ExtractionContext,
        segment: str,
        msg_type: MessageType,
        confidence: float,
    ) -> ExtractedMessage:
        """Create one ExtractedMessage from a classified segment."""
        return ExtractedMessage(
            id=uuid4(),
            agent_id=ctx.agent_id,
            channel_id=ctx.channel_id,
            group_id=ctx.group_id,
            session_id=ctx.session_id,
            type=msg_type,
            content=segment.strip(),
            content_length=len(segment.strip()),
            mentions=self._collect_mentions(segment),
            task_id=ctx.task_id,
            confidence=confidence,
            raw_excerpt=segment[:MAX_EXCERPT_LENGTH]
            if len(segment) > MAX_EXCERPT_LENGTH
            else segment,
        )

    def _segment_content(self, content: str) -> list[str]:
        """
        Segment content into logical chunks.

        Segmentation strategy:
        1. Split on double newlines (paragraphs)
        2. Split on code blocks
        3. Keep sentences together
        """
        segments: list[str] = []

        # First, handle code blocks specially
        code_block_pattern = re.compile(r"(```[\s\S]*?```)")
        parts = code_block_pattern.split(content)

        for part in parts:
            if part.startswith("```"):
                # Code block is its own segment
                segments.append(part)
            else:
                # Split non-code on double newlines
                paragraphs = re.split(r"\n\s*\n", part)
                for para in paragraphs:
                    if para.strip():
                        segments.append(para.strip())

        return segments

    def _classify_segment(
        self,
        segment: str,
    ) -> tuple[MessageType, float, list[str]]:
        """
        Classify a segment into a message type.

        Returns:
            Tuple of (MessageType, confidence, matched_patterns)
        """
        # Check each type's patterns
        type_scores: dict[MessageType, tuple[int, list[str]]] = {}

        for msg_type, patterns in self._compiled_patterns.items():
            matches: list[str] = []
            for pattern in patterns:
                if pattern.search(segment):
                    matches.append(pattern.pattern)

            if matches:
                type_scores[msg_type] = (len(matches), matches)

        if not type_scores:
            # Default to REASONING if no patterns match
            return MessageType.REASONING, 0.5, []

        # Get highest scoring type
        best_type = max(type_scores.keys(), key=lambda t: type_scores[t][0])
        match_count, matches = type_scores[best_type]

        # Calculate confidence based on match count
        total_patterns = len(self._compiled_patterns[best_type])
        confidence = min(1.0, (match_count / max(1, total_patterns)) + 0.5)

        return best_type, confidence, matches

    async def _decisions_segment_types(
        self, segments: list[str]
    ) -> list[tuple[str, float] | None]:
        """B10 segment_classify: the batched Decisions verdicts for a
        buffer, ALIGNED with ``segments`` (a None entry = that segment
        keeps the regex result). Empty list = no verdict anywhere
        (off/shadow/error/below floor), i.e. regex + fallback exactly as
        today. The alignment is load-bearing: extract() consumes this
        list positionally over the non-blank segments, so compacting the
        Nones out would shift verdicts onto the wrong segments."""
        if not segments:
            return []
        from roboco.config import settings

        if not settings.decisions_enabled:
            return []
        try:
            from roboco.services.decisions import pilots_content

            return list(await pilots_content.segment_classify(None, segments=segments))
        except Exception as exc:
            self.log.warning(
                "Decisions segment classify failed (fail-open)", error=str(exc)
            )
            return []


# =============================================================================
# PIPELINE
# =============================================================================


class ExtractionPipeline:
    """
    Complete pipeline from transcription buffer to stored messages.

    Combines TranscriptionService and ExtractionService for end-to-end
    processing of agent LLM streams.

    Usage:
        from roboco.services.transcription import TranscriptionService

        transcription = TranscriptionService()
        pipeline = ExtractionPipeline(transcription)

        await pipeline.start()

        # Messages are automatically extracted and callbacks invoked
        pipeline.on_message(lambda msg: store_message(msg))
    """

    def __init__(
        self,
        extraction_service: ExtractionService | None = None,
    ) -> None:
        self.extraction = extraction_service or ExtractionService()
        self._message_callbacks: list[Any] = []
        self.log = logger.bind(component="extraction_pipeline")

    def on_message(self, callback: Any) -> None:
        """Register a callback for extracted messages."""
        self._message_callbacks.append(callback)

    async def process_buffer(self, ctx: ExtractionContext) -> ExtractionResult:
        """
        Process a buffer and invoke callbacks for each message.
        """
        result = await self.extraction.extract(ctx)

        # Invoke callbacks for each message
        for message in result.messages:
            for callback in self._message_callbacks:
                try:
                    await callback(message)
                except Exception as e:
                    self.log.error(
                        "Message callback error",
                        error=str(e),
                        message_id=str(message.id),
                    )

        return result
