"""agent_core.people — relationship CRM with autonomy implications.

A small service object over the ``Person`` table. Skills + the action-policy
enforcer consult it to:

  - Look up who an inbound message is from / who an outbound is to.
  - Check ``autonomy_override`` + ``never_autonomous_send`` before acting.
  - Read ``stakeholder_class`` / ``relationship_intensity`` for prioritization.

The store is intentionally thin — most behavior lives in the consumer
(skill, policy, etc.). What the store guarantees:

  - Fingerprint dedup on ``name + organization`` to avoid duplicate Person
    rows when the same person is captured twice.
  - ``find_by_email`` / ``find_by_name`` lookups for the common cases.
  - Autonomy resolution: ``effective_autonomy(person, settings)`` collapses
    the per-person override + the install's default into a single
    PolicyKind so callers don't have to do the math.
"""

from agent_core.people.store import (
    PeopleStore,
    PersonNotFoundError,
    effective_autonomy,
)

__all__ = [
    "PeopleStore",
    "PersonNotFoundError",
    "effective_autonomy",
]
