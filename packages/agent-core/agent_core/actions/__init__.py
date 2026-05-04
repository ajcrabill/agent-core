"""agent_core.actions — action-class policy + daily digest.

Sprint 4.5 lands the policy layer and the digest synthesis. Later sprints
wire the policy enforcer into the agent loop's step-execution path (the
full enforcement story needs StepExecutor.declare() — a small refactor
when Hermes integrates).

Modules:
  policy.py    — ActionPolicy class with L10 defaults + per-instance overrides
  digest.py    — DailyDigestBuilder: action_log → human-readable summary
"""

from agent_core.actions.digest import (
    DIGEST_DELIVERY_SKILL,
    DailyDigest,
    DailyDigestBuilder,
    DigestDeliveryReport,
    deliver_digest,
)
from agent_core.actions.policy import (
    ActionPolicy,
    PolicyDecision,
    PolicyKind,
)

__all__ = [
    "ActionPolicy",
    "DIGEST_DELIVERY_SKILL",
    "DailyDigest",
    "DailyDigestBuilder",
    "DigestDeliveryReport",
    "PolicyDecision",
    "PolicyKind",
    "deliver_digest",
]
