"""email-composer skill.

Drafts an email reply (or a fresh outbound email). Three jobs:

  1. Match the recipient's level of formality. If they used your first name,
     use theirs. (Carried over from the professional seed pack.)
  2. Lead with what the reader needs; close with a clear next step.
  3. Stay in the user's voice — the LLM is prompted to reuse phrasings from
     openbrain hits when grounded.

The skill returns both subject and body so the caller can stuff them into
a draft directly. Confidence scaled by whether grounding context was
available.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from agent_core.skills import SeedRule, SkillContext, SkillResult

# ── Schemas ────────────────────────────────────────────────────────────────


class EmailComposerInput(BaseModel):
    to: str = Field(min_length=1, description="Recipient name + optional email")
    subject_hint: str = Field(
        default="",
        description="Optional subject seed; the skill may rewrite for clarity.",
    )
    brief: str = Field(min_length=1, description="What the email needs to convey")
    thread_history: str = Field(
        default="",
        description="Optional prior thread to keep tone + context continuity",
    )
    formality_hint: str = Field(
        default="auto",
        description="One of: auto|formal|casual — auto = match recipient cues",
    )
    ground_in_openbrain: bool = Field(
        default=False,
        description=(
            "If True, search semantic memory for related context (default off — "
            "drafting an email rarely needs broad recall, and grounding adds "
            "tokens + risk)."
        ),
    )


class EmailComposerOutput(BaseModel):
    subject: str
    body: str
    word_count: int


# ── Prompt ─────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You are an email drafter for a busy professional.

Write the email exactly as the user would send it. Three rules:

  1. Match the recipient's formality. If their last message used the user's
     first name, use theirs back. If they signed off "Best,", you sign off
     "Best,". Don't out-formal them; don't under-formal them.

  2. Lead with what the reader needs to know. Background later (or never).

  3. Close with a clear next step OR an explicit "no action needed".

Return EXACTLY this format (no preamble, no commentary):

  SUBJECT: <one line>
  ---
  <body, multiple paragraphs ok>

If a thread history was provided, your reply continues the thread —
don't restate context the recipient just sent.
"""


# ── Skill ───────────────────────────────────────────────────────────────────


class EmailComposer:
    """Draft an email response."""

    name = "email-composer"
    description = (
        "Draft an email reply matched to recipient formality + ending with a clear next step."
    )
    tags: list[str] = ["email", "draft", "writing"]
    input_schema = EmailComposerInput
    output_schema = EmailComposerOutput
    seed_rules: list[SeedRule] = [
        SeedRule(
            correction=(
                "Match the recipient's level of formality. If they used your "
                "first name, use theirs."
            ),
            skill_tags=["email-composer"],
        ),
        SeedRule(
            correction=(
                "Always include a clear next step (or explicit 'no action "
                "needed') in outbound communications."
            ),
            skill_tags=["email-composer"],
        ),
        SeedRule(
            correction=(
                "Use Oxford commas. Use em-dashes for asides — like this — not parentheses."
            ),
            skill_tags=["email-composer", "general"],
        ),
        SeedRule(
            correction=("Lead with what the reader needs to know. Background later."),
            skill_tags=["email-composer", "document-creator", "general"],
        ),
    ]

    def execute(self, input: EmailComposerInput, context: SkillContext) -> SkillResult:
        if context.language_model is None:
            raise RuntimeError("email-composer requires a LanguageModel in the SkillContext")

        # Optional grounding (off by default — emails are usually self-contained)
        references: list[dict[str, Any]] = []
        grounded_block = ""
        if input.ground_in_openbrain and context.openbrain is not None:
            query = f"{input.to} {input.brief}"
            hits = context.openbrain.search(query, limit=3)  # type: ignore[attr-defined]
            for i, hit in enumerate(hits, start=1):
                snippet = hit.thought.content[:200]
                src = hit.sources[0] if hit.sources else None
                references.append(
                    {
                        "n": i,
                        "snippet": snippet,
                        "source_kind": src.source_kind if src else None,
                        "source_uri": src.source_uri if src else None,
                        "similarity": round(hit.similarity, 3),
                    }
                )
            if references:
                grounded_block = "\nRecent relevant context:\n" + "\n".join(
                    f"[{r['n']}] {r['snippet']}" for r in references
                )

        thread_block = (
            f"\nThread history:\n{input.thread_history}\n" if input.thread_history else ""
        )
        formality_block = (
            f"\nFormality: {input.formality_hint}" if input.formality_hint != "auto" else ""
        )
        subject_block = f"Subject hint: {input.subject_hint}\n" if input.subject_hint else ""

        user_prompt = (
            f"To: {input.to}\n"
            f"{subject_block}"
            f"\n"
            f"Brief: {input.brief}"
            f"{formality_block}"
            f"{thread_block}"
            f"{grounded_block}"
        )

        raw = context.language_model.complete(  # type: ignore[attr-defined]
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=1000,
            temperature=0.4,
        )

        subject, body = _parse_response(raw, fallback_subject=input.subject_hint)
        word_count = len(body.split())

        # Confidence: full when grounded + thread context provided; lower
        # when the model had to invent everything from a bare brief.
        signals = sum(
            (
                bool(references),
                bool(input.thread_history),
                bool(input.subject_hint),
            )
        )
        confidence = 0.55 + 0.1 * signals  # 0.55 → 0.85 across 0-3 signals

        output = EmailComposerOutput(subject=subject, body=body, word_count=word_count)
        return SkillResult(
            output=output,
            confidence=confidence,
            rationale=f"drafted {word_count}w email to {input.to}",
            references=references,
        )


# ── Helpers ────────────────────────────────────────────────────────────────


_SUBJECT_LINE = re.compile(r"^\s*SUBJECT:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_DIVIDER = re.compile(r"^---\s*$", re.MULTILINE)


def _parse_response(raw: str, *, fallback_subject: str) -> tuple[str, str]:
    """Pull (subject, body) out of the LLM's structured response.

    Tolerant of small format drift: missing divider → split after the first
    SUBJECT line; missing SUBJECT → use the fallback subject."""
    raw = raw.strip()
    if not raw:
        raise ValueError("email-composer: empty model response")

    subject_match = _SUBJECT_LINE.search(raw)
    subject = (
        subject_match.group(1).strip() if subject_match else (fallback_subject or "(no subject)")
    )

    # Body = everything after the divider OR after the SUBJECT line.
    divider = _DIVIDER.search(raw)
    if divider:
        body = raw[divider.end() :].strip()
    elif subject_match:
        body = raw[subject_match.end() :].lstrip("\n-").strip()
    else:
        body = raw

    if not body:
        raise ValueError("email-composer: empty body in model response")
    return subject, body


__all__ = ["EmailComposer", "EmailComposerInput", "EmailComposerOutput"]
