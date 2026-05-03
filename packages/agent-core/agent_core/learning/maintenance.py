"""Maintenance scan — find duplicates, conflicts, stale, and compactable rules.

Run as part of the weekly review (Sprint 5b) or on demand.

What it finds:
  - **duplicates**: rules with very similar correction text (Jaccard token
    similarity > threshold). These are usually drift from a prior rule.
  - **conflicts**: rules with the same skill tag whose corrections share
    a leading verb but differ in object — often "use X" + "use Y" for the
    same thing. Heuristic only; LLM-driven detector lands later.
  - **stale**: never-fired rules + rules not fired in N days (delegates to
    RuleFirings.stale_rules).
  - **compactable**: 3+ rules sharing a tag that could plausibly merge. The
    heuristic flags clusters; the user (or an LLM) decides whether to merge.

The scan is read-only; it produces a MaintenanceReport but doesn't mutate
the store. Mutations (supersede / merge) happen via the LearningStore API
after user review.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from agent_core.learning.firings import RuleFirings
from agent_core.learning.store import LearningStore
from agent_core.state.db import Database
from agent_core.state.models import LearningRule

# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class DuplicateFinding:
    rule_a_id: str
    rule_b_id: str
    similarity: float
    rule_a_text: str
    rule_b_text: str


@dataclass
class ConflictFinding:
    rule_a_id: str
    rule_b_id: str
    shared_tag: str
    leading_verb: str
    rule_a_text: str
    rule_b_text: str


@dataclass
class CompactableCluster:
    tag: str
    rule_ids: list[str]
    sample_text: list[str]


@dataclass
class MaintenanceReport:
    duplicates: list[DuplicateFinding] = field(default_factory=list)
    conflicts: list[ConflictFinding] = field(default_factory=list)
    stale: list[LearningRule] = field(default_factory=list)
    compactable: list[CompactableCluster] = field(default_factory=list)
    rules_scanned: int = 0

    def has_findings(self) -> bool:
        return any((self.duplicates, self.conflicts, self.stale, self.compactable))

    def as_markdown(self) -> str:
        lines: list[str] = ["# Learning-rule maintenance scan", ""]
        lines.append(f"_Scanned **{self.rules_scanned}** active rules._")
        lines.append("")

        if not self.has_findings():
            lines.append("✓ No findings. Rule set looks healthy.")
            lines.append("")
            return "\n".join(lines)

        if self.duplicates:
            lines.append(f"## Possible duplicates ({len(self.duplicates)})")
            lines.append("")
            for d in self.duplicates:
                lines.append(
                    f"- similarity {d.similarity:.2f}: "
                    f"`{d.rule_a_id[:8]}` _{d.rule_a_text[:80]}_ vs "
                    f"`{d.rule_b_id[:8]}` _{d.rule_b_text[:80]}_"
                )
            lines.append("")

        if self.conflicts:
            lines.append(f"## Possible conflicts ({len(self.conflicts)})")
            lines.append("")
            for c in self.conflicts:
                lines.append(
                    f"- tag `{c.shared_tag}`, both start with `{c.leading_verb}`: "
                    f"`{c.rule_a_id[:8]}` _{c.rule_a_text[:80]}_ vs "
                    f"`{c.rule_b_id[:8]}` _{c.rule_b_text[:80]}_"
                )
            lines.append("")

        if self.stale:
            lines.append(f"## Stale (not fired recently or never) ({len(self.stale)})")
            lines.append("")
            for r in self.stale:
                lines.append(f"- `{r.id[:8]}` _{r.correction[:100]}_ (tags: {r.skill_tags})")
            lines.append("")

        if self.compactable:
            lines.append(f"## Compactable clusters ({len(self.compactable)})")
            lines.append("")
            for cluster in self.compactable:
                lines.append(
                    f"- tag `{cluster.tag}` has {len(cluster.rule_ids)} rules — "
                    f"could possibly merge:"
                )
                for txt in cluster.sample_text[:5]:
                    lines.append(f"  - _{txt[:100]}_")
            lines.append("")

        return "\n".join(lines)


# ── Scanner ──────────────────────────────────────────────────────────────────


class MaintenanceScan:
    """Run the four checks against the active learning rules.

    Tunables:
      duplicate_threshold: Jaccard similarity above which rules are flagged
        as possible duplicates (default 0.7)
      stale_days: rules untouched this long flagged (default 90)
      compactable_min_cluster: how many rules per (tag) before flagging
        as a candidate for compaction (default 5)
    """

    def __init__(
        self,
        db: Database,
        store: LearningStore | None = None,
        firings: RuleFirings | None = None,
        *,
        duplicate_threshold: float = 0.7,
        stale_days: int = 90,
        compactable_min_cluster: int = 5,
    ) -> None:
        self.db = db
        self.store = store or LearningStore(db, write_ahead=False)
        self.firings = firings or RuleFirings(db)
        self.duplicate_threshold = duplicate_threshold
        self.stale_days = stale_days
        self.compactable_min_cluster = compactable_min_cluster

    def run(self) -> MaintenanceReport:
        """Run all four scans, return a single report."""
        rules = self.store.list_active()
        report = MaintenanceReport(rules_scanned=len(rules))
        report.duplicates = self._find_duplicates(rules)
        report.conflicts = self._find_conflicts(rules)
        report.stale = self.firings.stale_rules(days=self.stale_days)
        report.compactable = self._find_compactable(rules)
        return report

    # ── Duplicate scan ─────────────────────────────────────────────────────

    def _find_duplicates(self, rules: list[LearningRule]) -> list[DuplicateFinding]:
        out: list[DuplicateFinding] = []
        token_sets = [(r, _tokenize(r.correction)) for r in rules]
        for i, (rule_a, tok_a) in enumerate(token_sets):
            for rule_b, tok_b in token_sets[i + 1 :]:
                sim = _jaccard(tok_a, tok_b)
                if sim >= self.duplicate_threshold:
                    out.append(
                        DuplicateFinding(
                            rule_a_id=rule_a.id,
                            rule_b_id=rule_b.id,
                            similarity=sim,
                            rule_a_text=rule_a.correction,
                            rule_b_text=rule_b.correction,
                        )
                    )
        # Highest similarity first
        out.sort(key=lambda d: d.similarity, reverse=True)
        return out

    # ── Conflict scan ─────────────────────────────────────────────────────

    def _find_conflicts(self, rules: list[LearningRule]) -> list[ConflictFinding]:
        """Heuristic: rules sharing a skill tag whose corrections start with
        the same imperative verb (use/avoid/include/exclude/etc.) but differ
        in object. Often a clue that they say opposite things."""
        out: list[ConflictFinding] = []
        # Bucket by (tag, leading_verb)
        bucket: dict[tuple[str, str], list[LearningRule]] = defaultdict(list)
        for r in rules:
            verb = _leading_verb(r.correction)
            if not verb:
                continue
            for tag in r.skill_tags or []:
                bucket[(tag, verb)].append(r)

        for (tag, verb), entries in bucket.items():
            if len(entries) < 2:
                continue
            # Pair every two rules in the bucket; conflict only if the
            # token-similarity is LOW (otherwise it's a duplicate, not a
            # conflict). Threshold: < 0.4 token Jaccard = "different content".
            for i, ra in enumerate(entries):
                tok_a = _tokenize(ra.correction)
                for rb in entries[i + 1 :]:
                    tok_b = _tokenize(rb.correction)
                    if _jaccard(tok_a, tok_b) >= 0.4:
                        continue
                    out.append(
                        ConflictFinding(
                            rule_a_id=ra.id,
                            rule_b_id=rb.id,
                            shared_tag=tag,
                            leading_verb=verb,
                            rule_a_text=ra.correction,
                            rule_b_text=rb.correction,
                        )
                    )
        return out

    # ── Compactable scan ───────────────────────────────────────────────────

    def _find_compactable(self, rules: list[LearningRule]) -> list[CompactableCluster]:
        """Tags with a lot of rules are candidates for compaction (e.g., if
        you have 8 separate "BD email" rules, maybe they should consolidate
        into 1 multi-clause rule)."""
        by_tag: dict[str, list[LearningRule]] = defaultdict(list)
        for r in rules:
            for tag in r.skill_tags or []:
                by_tag[tag].append(r)
        out: list[CompactableCluster] = []
        for tag, lst in by_tag.items():
            if tag == "general":
                # Too broad; skip — general rules are ALL legitimately distinct
                continue
            if len(lst) >= self.compactable_min_cluster:
                out.append(
                    CompactableCluster(
                        tag=tag,
                        rule_ids=[r.id for r in lst],
                        sample_text=[r.correction for r in lst],
                    )
                )
        return out


# ── Text helpers ─────────────────────────────────────────────────────────────


_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> set[str]:
    """Lowercased word tokens, with common stopwords stripped (so 'use the X'
    and 'use X' have similar token sets)."""
    return set(_TOKEN_RE.findall(text.lower())) - _STOPWORDS


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _leading_verb(text: str) -> str | None:
    """First word, lowercased. Used for conflict bucketing — two rules that
    both start with 'use' or 'avoid' on the same tag are worth comparing."""
    text = text.strip().lstrip("*-•").strip()
    if not text:
        return None
    first = text.split()[0].lower().rstrip(".,:;!?")
    return first if first.isalpha() else None


_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "is",
    "are",
    "was",
    "be",
    "been",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "should",
    "could",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "them",
    "they",
    "their",
    "i",
    "me",
    "my",
    "you",
    "your",
    "we",
    "our",
    "us",
}


__all__ = [
    "CompactableCluster",
    "ConflictFinding",
    "DuplicateFinding",
    "MaintenanceReport",
    "MaintenanceScan",
]
