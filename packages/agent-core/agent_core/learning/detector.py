"""Correction detector — spot principal corrections in chat, surface as candidates.

When the principal corrects the agent in chat ("no, do it like X", "stop
doing Y", "actually use Z"), the detector should notice and create a
CorrectionCandidate so the user can one-click promote it to a learning rule
during the weekly review.

This module ships:

  CorrectionDetector — Protocol for LLM-driven detection (real impl when
                       Hermes vendors)
  HeuristicDetector  — pattern-based fallback that runs without a model;
                       catches obvious cases ("don't do X", "use Y instead",
                       etc.) without false-positives on neutral chat.

The chat layer wires the detector into incoming messages. Each detected
candidate flows into CorrectionCandidates.propose().
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class DetectedCorrection:
    """What a detector returns when it spots a correction. The chat layer
    feeds this into CorrectionCandidates.propose()."""

    correction_text: str
    inferred_skill_tags: list[str]
    confidence: float  # 0.0–1.0
    source_excerpt: str  # the verbatim user text that triggered detection


@runtime_checkable
class CorrectionDetector(Protocol):
    """Inspect a chat exchange; return a DetectedCorrection if one is
    present, else None.

    Real implementations call a model with a "is this a correction?" prompt
    and produce a structured result. Heuristic implementations (below) use
    text patterns.
    """

    def detect(
        self,
        *,
        principal_message: str,
        agent_previous_action: str | None = None,
        skill_in_context: str | None = None,
    ) -> DetectedCorrection | None: ...


# ── Heuristic detector ───────────────────────────────────────────────────────


# Patterns that strongly suggest the principal is correcting / instructing
# rather than just chatting. Tuned to be conservative — false-positives are
# worse than false-negatives because users get review fatigue.
_CORRECTION_PATTERNS: list[tuple[re.Pattern, float]] = [
    # Direct prohibitions: "don't/stop/never <verb-ish>"
    # The second word can be anything word-like — "don't ever do X" / "stop using Y"
    # / "never include Z" all match.
    (re.compile(r"\b(don'?t|do not|stop|never)\s+\w+", re.I), 0.85),
    # "use X instead of Y" / "use X not Y" / "use X, not Y"
    # Allow up to ~60 chars between 'use' and 'not/instead of/rather than' so
    # multi-word objects ("use percentage delta, not absolute dollars") work.
    (re.compile(r"\buse\b.{1,60}?\b(instead\s+of|not|rather\s+than)\b", re.I), 0.9),
    # "always X" / "from now on"
    (re.compile(r"\b(from now on|going forward|always)\b", re.I), 0.75),
    # "actually X" / "no, X" — comma optional after "actually" or "no"
    (re.compile(r"^(actually,?|no,?)\s+", re.I), 0.65),
    # Explicit framing
    (re.compile(r"\b(remember (to|that)|please note|note that)\b", re.I), 0.7),
    (re.compile(r"\bnext time\b", re.I), 0.8),
    # Reversal
    (re.compile(r"\b(prefer|rather)\b.*\b(over|than)\b", re.I), 0.75),
]


class HeuristicDetector:
    """Pattern-based correction detector. Runs without an LLM.

    Use as the default detector until the LLM-backed detector lands. Catches
    obvious explicit corrections; misses subtle ones (those the LLM-backed
    detector picks up later).

    Tunables:
      min_confidence: drop matches below this threshold (default 0.6)
      max_excerpt_chars: truncate source_excerpt at this length (default 240)
    """

    def __init__(
        self,
        *,
        min_confidence: float = 0.6,
        max_excerpt_chars: int = 240,
    ) -> None:
        self.min_confidence = min_confidence
        self.max_excerpt_chars = max_excerpt_chars

    def detect(
        self,
        *,
        principal_message: str,
        agent_previous_action: str | None = None,
        skill_in_context: str | None = None,
    ) -> DetectedCorrection | None:
        text = principal_message.strip()
        if not text:
            return None

        # Find the strongest matching pattern; use its confidence
        best_confidence = 0.0
        for pattern, conf in _CORRECTION_PATTERNS:
            if pattern.search(text):
                best_confidence = max(best_confidence, conf)
        if best_confidence < self.min_confidence:
            return None

        # Build correction text: just the user's message, lightly cleaned.
        # The LLM detector would synthesize a rule here; the heuristic just
        # surfaces the raw user text and lets the user edit it on promote.
        correction_text = text
        if len(correction_text) > self.max_excerpt_chars:
            correction_text = correction_text[: self.max_excerpt_chars - 1] + "…"

        # Excerpt = the same text (since this is purely the message).
        excerpt = text[: self.max_excerpt_chars]

        # Tag scope: prefer the active skill if one was named; else 'general'.
        tags = [skill_in_context] if skill_in_context else ["general"]

        return DetectedCorrection(
            correction_text=correction_text,
            inferred_skill_tags=tags,
            confidence=best_confidence,
            source_excerpt=excerpt,
        )


__all__ = [
    "CorrectionDetector",
    "DetectedCorrection",
    "HeuristicDetector",
]
