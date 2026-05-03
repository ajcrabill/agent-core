"""document-creator skill.

Drafts a document from a brief, optionally grounded in semantic context
pulled from openbrain. Intentionally generic — the brief drives the
document type (memo, briefing, evaluation, etc.). Voice + style come from
seed rules + the user's accumulated learning rules.

The skill consults openbrain when ``ground_in_openbrain`` is True (default):
it embeds ``brief`` + ``title`` and pulls top-K relevant thoughts; those go
into the prompt as cited context. The skill returns the citations on the
SkillResult so the UI can show provenance.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agent_core.skills import SeedRule, SkillContext, SkillResult


# ── Schemas ────────────────────────────────────────────────────────────────


class DocumentCreatorInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, description="What the document needs to do")
    audience: str = Field(default="", description="Who reads this; shapes tone + depth")
    length_target: str = Field(
        default="brief",
        description="One of: brief|standard|long|exhaustive — guides word count.",
    )
    ground_in_openbrain: bool = Field(
        default=True,
        description="If True, pull relevant context from semantic memory before drafting.",
    )
    additional_context: str = Field(
        default="",
        description="Caller-supplied facts the LLM should weave in.",
    )


class DocumentCreatorOutput(BaseModel):
    title: str
    body: str
    word_count: int


# ── Prompt ─────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """You are a document writer for a busy professional.

Write the document the user asks for. Match the requested length target.
If the user provided audience guidance, calibrate tone and depth to that
audience. If grounded context is provided, weave the relevant facts in
naturally — don't dump them. Cite sources inline as [n] referring to the
numbered list in the user message.

Return ONLY the document body. No meta-commentary, no preamble like
"Here's the draft:". The first line should be the document's first line.
"""


_LENGTH_GUIDANCE = {
    "brief": "Keep under 200 words. One short paragraph or three tight bullets.",
    "standard": "300-500 words. Clear structure with at most one heading.",
    "long": "800-1200 words. Use headings; multiple sections.",
    "exhaustive": "1500+ words. Comprehensive; sub-sections; nothing dropped.",
}


# ── Skill ───────────────────────────────────────────────────────────────────


class DocumentCreator:
    """Draft a document from a brief."""

    name = "document-creator"
    description = "Draft a document from a brief, optionally grounded in openbrain context."
    tags: list[str] = ["document", "writing", "draft"]
    input_schema = DocumentCreatorInput
    output_schema = DocumentCreatorOutput
    seed_rules: list[SeedRule] = [
        SeedRule(
            correction=(
                "Lead with what the reader needs to know. Save background "
                "for later paragraphs."
            ),
            skill_tags=["document-creator"],
        ),
        SeedRule(
            correction=(
                "Cite every concrete claim. If you can't cite, hedge ('roughly', "
                "'estimated') or omit."
            ),
            skill_tags=["document-creator"],
        ),
        SeedRule(
            correction=(
                "Match the requested length target. A 'brief' draft over 200 "
                "words is wrong even if every word is good."
            ),
            skill_tags=["document-creator"],
        ),
    ]

    def execute(self, input: DocumentCreatorInput, context: SkillContext) -> SkillResult:
        if context.language_model is None:
            raise RuntimeError(
                "document-creator requires a LanguageModel in the SkillContext"
            )

        # Optional grounding from openbrain.
        references: list[dict[str, Any]] = []
        grounded_block = ""
        if input.ground_in_openbrain and context.openbrain is not None:
            query = f"{input.title}\n{input.brief}"
            hits = context.openbrain.search(query, limit=5)  # type: ignore[attr-defined]
            for i, hit in enumerate(hits, start=1):
                snippet = hit.thought.content[:300]
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
                grounded_lines = [
                    f"[{r['n']}] ({r.get('source_kind') or 'unknown'}) {r['snippet']}"
                    for r in references
                ]
                grounded_block = "\nRelevant context from your memory:\n" + "\n".join(
                    grounded_lines
                )

        length_hint = _LENGTH_GUIDANCE.get(
            input.length_target, _LENGTH_GUIDANCE["standard"]
        )
        audience_hint = (
            f"\nAudience: {input.audience}" if input.audience else ""
        )
        extra_hint = (
            f"\n\nAdditional context to use:\n{input.additional_context}"
            if input.additional_context
            else ""
        )

        user_prompt = (
            f"Title: {input.title}\n"
            f"Length target: {input.length_target} ({length_hint}){audience_hint}\n"
            f"\n"
            f"Brief: {input.brief}"
            f"{extra_hint}"
            f"{grounded_block}"
        )

        body = context.language_model.complete(  # type: ignore[attr-defined]
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=2000,
            temperature=0.3,
        ).strip()

        word_count = len(body.split())
        # Confidence proxy: did we ground the document, and did we hit the
        # length target? Both nudges confidence up; mismatches nudge it down.
        confidence = _estimate_confidence(
            grounded=bool(references),
            length_target=input.length_target,
            actual_words=word_count,
        )

        output = DocumentCreatorOutput(
            title=input.title, body=body, word_count=word_count
        )
        return SkillResult(
            output=output,
            confidence=confidence,
            rationale=(
                f"drafted {word_count}w document (target={input.length_target}, "
                f"grounded={bool(references)})"
            ),
            references=references,
        )


# ── Helpers ────────────────────────────────────────────────────────────────


_LENGTH_RANGES = {
    "brief": (1, 200),
    "standard": (200, 600),
    "long": (700, 1300),
    "exhaustive": (1300, 5000),
}


def _estimate_confidence(*, grounded: bool, length_target: str, actual_words: int) -> float:
    """Heuristic confidence — high when grounded + on-target length, lower
    when the LLM blew past the requested length budget."""
    base = 0.7 if grounded else 0.55
    lo, hi = _LENGTH_RANGES.get(length_target, (1, 5000))
    if lo <= actual_words <= hi:
        return min(1.0, base + 0.15)
    # Outside target range → drop confidence proportional to how far off.
    if actual_words < lo:
        return max(0.2, base - 0.2)
    return max(0.2, base - 0.3)


__all__ = ["DocumentCreator", "DocumentCreatorInput", "DocumentCreatorOutput"]
