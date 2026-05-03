"""email-triage skill.

Classifies an inbound email into one of six actions, with confidence and a
short reasoning string. Action taxonomy lifted directly from the user's
vault learning-log:

    flag                 — requires AJ decision or response
    archive              — newsletters, confirmations, FYI; safe to archive
    hold                 — important but not urgent; revisit in 2-3 days
    draft                — needs a reply drafted (delegate to email-composer)
    track-relationship   — update People notes with contact info
    task                 — create / link a task

Confidence buckets per the same vault doc:
    high   — score >= 0.80
    medium — score in [0.50, 0.80)
    low    — score < 0.50

Implementation: a single LLM call with the action taxonomy as a structured
prompt. The skill is intentionally schema-strict on the LLM's output (parses
JSON; fails closed if the model hallucinates an action outside the taxonomy)
so the calibration loop can trust the confidence values it receives."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from agent_core.skills import SeedRule, SkillContext, SkillResult


TriageAction = Literal[
    "flag",
    "archive",
    "hold",
    "draft",
    "track-relationship",
    "task",
]


# ── Schemas ────────────────────────────────────────────────────────────────


class EmailTriageInput(BaseModel):
    sender: str
    subject: str
    body: str
    message_id: str | None = None
    received_at: str | None = None


class EmailTriageOutput(BaseModel):
    action: TriageAction
    confidence_bucket: Literal["high", "medium", "low"]
    reasoning: str = Field(default="")


# ── Prompt ─────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You are an email triage classifier.

Classify the inbound email into EXACTLY one of these six actions:

  flag                 — requires the user's decision or personal response
  archive              — newsletters, confirmations, FYI; safe to archive
  hold                 — important but not urgent; revisit in 2-3 days
  draft                — needs a reply drafted by the agent
  track-relationship   — sender info worth recording in People notes
  task                 — create or link to a task in projects

Return ONLY a JSON object with exactly these keys:

  {
    "action": "<one of the six>",
    "score": <number 0.0-1.0>,
    "reasoning": "<one short sentence>"
  }

No prose around the JSON. No markdown fence. Just the object.
"""


# ── Skill ───────────────────────────────────────────────────────────────────


class EmailTriage:
    """Classify inbound email by action."""

    name = "email-triage"
    description = "Classify an inbound email by action (flag/archive/hold/draft/track-relationship/task)."
    tags: list[str] = ["email", "classify", "triage"]
    input_schema = EmailTriageInput
    output_schema = EmailTriageOutput
    seed_rules: list[SeedRule] = [
        SeedRule(
            correction=(
                "Newsletters, calendar reminders, and automated notifications archive "
                "by default. Only flag if there's a deadline within 24h."
            ),
            skill_tags=["email-triage"],
        ),
        SeedRule(
            correction=(
                "When a sender is new (no prior thread) and the email is non-promotional, "
                "set 'track-relationship' so contact info gets recorded."
            ),
            skill_tags=["email-triage"],
        ),
        SeedRule(
            correction=(
                "If the email contains an explicit question directed at the user, "
                "the action is 'flag' (or 'draft' if a reply is obviously expected)."
            ),
            skill_tags=["email-triage"],
        ),
    ]

    def execute(self, input: EmailTriageInput, context: SkillContext) -> SkillResult:
        if context.language_model is None:
            raise RuntimeError(
                "email-triage requires a LanguageModel in the SkillContext"
            )

        user_prompt = (
            f"From: {input.sender}\n"
            f"Subject: {input.subject}\n"
            f"\n"
            f"{input.body}"
        )
        raw = context.language_model.complete(  # type: ignore[attr-defined]
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=200,
            temperature=0.0,
        )
        parsed = _parse_response(raw)

        confidence = float(parsed.get("score", 0.0))
        action = parsed.get("action", "")
        reasoning = parsed.get("reasoning", "")

        # Validate the action against the literal type — fail closed on
        # hallucinated values so calibration doesn't get poisoned.
        if action not in (
            "flag",
            "archive",
            "hold",
            "draft",
            "track-relationship",
            "task",
        ):
            raise ValueError(
                f"email-triage: model returned unknown action {action!r}"
            )

        # Map score → bucket per vault confidence-bucket conventions.
        if confidence >= 0.80:
            bucket = "high"
        elif confidence >= 0.50:
            bucket = "medium"
        else:
            bucket = "low"

        output = EmailTriageOutput(
            action=action,  # type: ignore[arg-type]
            confidence_bucket=bucket,  # type: ignore[arg-type]
            reasoning=reasoning,
        )
        return SkillResult(
            output=output,
            confidence=confidence,
            rationale=reasoning,
        )


# ── Helpers ────────────────────────────────────────────────────────────────


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_response(raw: str) -> dict:
    """Parse the LLM's JSON response. Handles bare JSON or fenced JSON
    (model occasionally wraps in ```json``` despite instruction)."""
    raw = raw.strip()
    if not raw:
        raise ValueError("email-triage: empty model response")
    # Bare JSON path — fast common case
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Fenced fallback
    m = _JSON_FENCE.search(raw)
    if m:
        return json.loads(m.group(1))
    raise ValueError(f"email-triage: could not parse JSON from response: {raw!r}")


__all__ = ["EmailTriage", "EmailTriageInput", "EmailTriageOutput", "TriageAction"]
