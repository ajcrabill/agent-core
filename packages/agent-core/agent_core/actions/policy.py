"""ActionPolicy — encodes locked decision L10.

Three classes of action policy:

  autonomous: agent does it without confirmation; it lands in action_log + the
    daily digest, surfaced after the fact (per L9 reporting cadence).

  gated: agent prepares the action and surfaces it for one-click human
    confirmation. Gated actions don't execute until the principal approves.

  forbidden: agent refuses entirely. The package is structurally incapable
    of doing these (the policy enforcer must short-circuit before any tool
    call). Examples: direct secret access, financial transactions, or
    anything that would require modifying the safety-rule set itself.

Defaults (L10):

  autonomous:
    read, write_internal, ob_update, cross_agent_message,
    calendar_read, ingest, capture_learning_candidate

  gated:
    send_email_external, content_publish, calendar_invite_external,
    modify_people_data, install_skill

  forbidden:
    secret_access, finance

Overrides:
  Users can adjust per-class via Tier 3 of the wizard (e.g., promote
  send_email_external to autonomous for a specific install). Overrides are
  passed in at construction or applied via .set(). Forbidden classes can be
  promoted to gated/autonomous by explicit user override (the policy doesn't
  hardcode the forbidden list — it's just the default), but the safety-
  rule set in the model harness still refuses certain operations regardless.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agent_core.state.models import ActionClass


class PolicyKind(StrEnum):
    autonomous = "autonomous"
    gated = "gated"
    forbidden = "forbidden"


# Locked defaults per L10
_DEFAULTS: dict[ActionClass, PolicyKind] = {
    # Autonomous
    ActionClass.read: PolicyKind.autonomous,
    ActionClass.write_internal: PolicyKind.autonomous,
    ActionClass.ob_update: PolicyKind.autonomous,
    ActionClass.cross_agent_message: PolicyKind.autonomous,
    ActionClass.calendar_read: PolicyKind.autonomous,
    ActionClass.ingest: PolicyKind.autonomous,
    ActionClass.capture_learning_candidate: PolicyKind.autonomous,
    # Gated
    ActionClass.send_email_external: PolicyKind.gated,
    ActionClass.content_publish: PolicyKind.gated,
    ActionClass.calendar_invite_external: PolicyKind.gated,
    ActionClass.modify_people_data: PolicyKind.gated,
    ActionClass.install_skill: PolicyKind.gated,
    # Forbidden
    ActionClass.secret_access: PolicyKind.forbidden,
    ActionClass.finance: PolicyKind.forbidden,
}


@dataclass
class PolicyDecision:
    """Outcome of asking the policy about a specific action."""

    action_class: ActionClass
    kind: PolicyKind
    reason: str

    @property
    def is_autonomous(self) -> bool:
        return self.kind == PolicyKind.autonomous

    @property
    def is_gated(self) -> bool:
        return self.kind == PolicyKind.gated

    @property
    def is_forbidden(self) -> bool:
        return self.kind == PolicyKind.forbidden


class ActionPolicy:
    """Map ActionClass → PolicyKind with sensible defaults + user overrides.

    The defaults are L10. Pass `overrides` at construction (or call .set())
    to deviate per install — e.g., promote send_email_external to autonomous
    if the user trusts the email pipeline:

        policy = ActionPolicy(overrides={
            ActionClass.send_email_external: PolicyKind.autonomous,
        })
    """

    def __init__(
        self,
        overrides: dict[ActionClass, PolicyKind] | None = None,
    ) -> None:
        self._policies: dict[ActionClass, PolicyKind] = dict(_DEFAULTS)
        if overrides:
            self._policies.update(overrides)

    # ── Read API ────────────────────────────────────────────────────────────

    def policy_for(self, action_class: ActionClass) -> PolicyKind:
        """Return the policy kind for ``action_class``.

        Unknown action classes (e.g., a future enum value not in the defaults
        map) default to ``gated`` — refuse to run silently; surface to the
        human.
        """
        return self._policies.get(action_class, PolicyKind.gated)

    def decide(self, action_class: ActionClass) -> PolicyDecision:
        """Return a structured decision (kind + human-readable reason)."""
        kind = self.policy_for(action_class)
        if kind == PolicyKind.autonomous:
            reason = f"{action_class.value} is in the autonomous default set"
        elif kind == PolicyKind.gated:
            reason = f"{action_class.value} requires explicit human confirmation"
        else:
            reason = f"{action_class.value} is forbidden by policy"
        return PolicyDecision(action_class=action_class, kind=kind, reason=reason)

    def is_autonomous(self, action_class: ActionClass) -> bool:
        return self.policy_for(action_class) == PolicyKind.autonomous

    def is_gated(self, action_class: ActionClass) -> bool:
        return self.policy_for(action_class) == PolicyKind.gated

    def is_forbidden(self, action_class: ActionClass) -> bool:
        return self.policy_for(action_class) == PolicyKind.forbidden

    # ── Mutation ────────────────────────────────────────────────────────────

    def set(self, action_class: ActionClass, kind: PolicyKind) -> None:
        """Override the policy for ``action_class``. Useful for runtime tweaks
        and for the wizard's Tier 3 setup."""
        self._policies[action_class] = kind

    def reset_to_default(self, action_class: ActionClass) -> None:
        """Restore L10 default for one action class."""
        if action_class in _DEFAULTS:
            self._policies[action_class] = _DEFAULTS[action_class]
        else:
            self._policies.pop(action_class, None)

    # ── Bulk ────────────────────────────────────────────────────────────────

    def as_dict(self) -> dict[str, str]:
        """Snapshot of the current policy as a plain dict — for serialization
        to the user's config file."""
        return {ac.value: kind.value for ac, kind in self._policies.items()}

    @classmethod
    def from_dict(cls, mapping: dict[str, str]) -> ActionPolicy:
        """Rebuild from a dict (e.g., loaded from config)."""
        overrides: dict[ActionClass, PolicyKind] = {}
        for ac_name, kind_name in mapping.items():
            try:
                overrides[ActionClass(ac_name)] = PolicyKind(kind_name)
            except ValueError:
                # Tolerate unknown enum values (forward-compat)
                continue
        return cls(overrides=overrides)


__all__ = ["ActionPolicy", "PolicyDecision", "PolicyKind"]
