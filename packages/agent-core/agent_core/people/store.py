"""PeopleStore — CRUD + lookups + autonomy resolution for Person rows.

Thin layer over the ``person`` table. Five operations cover ~all caller
needs:

  - ``upsert(name, ...)``         — create or update by name+org fingerprint
  - ``get(person_id)``            — by ID
  - ``find_by_name(name)``        — exact (case-insensitive) match
  - ``find_by_email(addr)``       — searches contact_methods.email
  - ``list(*, stakeholder_class=, never_autonomous_send=)`` — filtered list

Plus ``effective_autonomy(person, settings)`` — the function skills + the
action-policy enforcer call before acting on someone's behalf.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import select

from agent_core.state.db import Database
from agent_core.state.models import AutonomyOverride, Person, utcnow

logger = logging.getLogger(__name__)


class PersonNotFoundError(KeyError):
    """Raised by ``require()`` when no Person matches the lookup."""


class PeopleStore:
    """CRUD + lookups for Person rows."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Create / update ─────────────────────────────────────────────────────

    def upsert(
        self,
        *,
        name: str,
        organization: str | None = None,
        role: str | None = None,
        stakeholder_class: str = "unknown_external",
        autonomy_override: AutonomyOverride = AutonomyOverride.inherit,
        relationship_intensity: int | None = None,
        response_sla: str | None = None,
        never_autonomous_send: bool = False,
        sensitive_memory_flag: bool = False,
        contact_methods: dict[str, Any] | None = None,
        notes_path: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> Person:
        """Create-or-update by (name, organization). Two people with the
        same name at different orgs are different rows; same name same org
        (case-insensitive on both) updates in place."""
        if not name or not name.strip():
            raise ValueError("Person.name is required")
        existing = self._find_by_name_org(name, organization)
        if existing is not None:
            return self._update(
                existing,
                role=role,
                stakeholder_class=stakeholder_class,
                autonomy_override=autonomy_override,
                relationship_intensity=relationship_intensity,
                response_sla=response_sla,
                never_autonomous_send=never_autonomous_send,
                sensitive_memory_flag=sensitive_memory_flag,
                contact_methods=contact_methods,
                notes_path=notes_path,
                metadata_json=metadata_json,
            )
        return self._create(
            name=name.strip(),
            organization=organization,
            role=role,
            stakeholder_class=stakeholder_class,
            autonomy_override=autonomy_override,
            relationship_intensity=relationship_intensity,
            response_sla=response_sla,
            never_autonomous_send=never_autonomous_send,
            sensitive_memory_flag=sensitive_memory_flag,
            contact_methods=contact_methods,
            notes_path=notes_path,
            metadata_json=metadata_json,
        )

    def _create(self, **fields: Any) -> Person:
        person = Person(**{k: v for k, v in fields.items() if v is not None or k == "metadata_json"})
        if not person.contact_methods:
            person.contact_methods = {}
        with self.db.session() as s:
            s.add(person)
            s.commit()
            s.refresh(person)
        logger.info("created person: id=%s name=%s", person.id[:8], person.name)
        return person

    def _update(self, person: Person, **fields: Any) -> Person:
        with self.db.session() as s:
            row = s.get(Person, person.id)
            for k, v in fields.items():
                if v is None:
                    continue
                setattr(row, k, v)
            row.updated_at = utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
        return row

    # ── Read ────────────────────────────────────────────────────────────────

    def get(self, person_id: str) -> Person | None:
        with self.db.session() as s:
            return s.get(Person, person_id)

    def require(self, person_id: str) -> Person:
        person = self.get(person_id)
        if person is None:
            raise PersonNotFoundError(f"no person with id={person_id!r}")
        return person

    def find_by_name(self, name: str) -> list[Person]:
        """Case-insensitive exact-name match. Returns 0+ rows; multiple
        people with the same name at different organizations land in the
        same list."""
        with self.db.session() as s:
            stmt = select(Person)
            return [
                p
                for p in s.exec(stmt).all()
                if p.name.lower() == name.lower()
            ]

    def find_by_email(self, address: str) -> Person | None:
        """Find by any matching contact_methods entry of kind ``email``.

        Linear scan — fine for personal-CoS scale (hundreds of people).
        Flip to an indexed contact-method table if iKB-scale demands it."""
        addr = address.strip().lower()
        if not addr:
            return None
        with self.db.session() as s:
            for person in s.exec(select(Person)).all():
                cm = person.contact_methods or {}
                value = cm.get("email")
                if isinstance(value, str) and value.lower() == addr:
                    return person
                # Tolerate list-of-emails for people with multiple addresses
                if isinstance(value, list):
                    if any(isinstance(v, str) and v.lower() == addr for v in value):
                        return person
        return None

    def _find_by_name_org(self, name: str, organization: str | None) -> Person | None:
        target_name = name.strip().lower()
        target_org = (organization or "").strip().lower() or None
        with self.db.session() as s:
            for p in s.exec(select(Person)).all():
                if p.name.lower() != target_name:
                    continue
                p_org = (p.organization or "").strip().lower() or None
                if p_org == target_org:
                    return p
        return None

    def list(
        self,
        *,
        stakeholder_class: str | None = None,
        never_autonomous_send: bool | None = None,
        autonomy_override: AutonomyOverride | None = None,
    ) -> list[Person]:
        with self.db.session() as s:
            stmt = select(Person).order_by(Person.name)
            if stakeholder_class is not None:
                stmt = stmt.where(Person.stakeholder_class == stakeholder_class)
            if never_autonomous_send is not None:
                stmt = stmt.where(Person.never_autonomous_send == never_autonomous_send)
            if autonomy_override is not None:
                stmt = stmt.where(Person.autonomy_override == autonomy_override)
            return list(s.exec(stmt).all())

    def count(self) -> int:
        with self.db.session() as s:
            return len(list(s.exec(select(Person)).all()))


# ── Autonomy resolution ────────────────────────────────────────────────────


# Map preset → numeric "autonomy notch" so we can apply +/- one notch overrides.
_PRESET_NOTCH = {"cautious": 0, "balanced": 1, "aggressive": 2}
_NOTCH_PRESET = {0: "cautious", 1: "balanced", 2: "aggressive"}


def effective_autonomy(person: Person, settings: object) -> str:
    """Return the resolved autonomy preset name for actions involving ``person``.

    Combines:
      - the install's settings.autonomy.default_policy (cautious|balanced|aggressive)
      - the person's autonomy_override (inherit|more_cautious|more_aggressive|never_autonomous)

    Rules:
      - never_autonomous always returns "cautious" regardless of preset
      - more_cautious shifts one notch toward cautious (clamped)
      - more_aggressive shifts one notch toward aggressive (clamped)
      - inherit returns the install default unchanged

    Returns the preset name as a string, not a PolicyKind — callers that
    need the per-action policy should ask ActionPolicy.policy_for() with
    this result feeding into preset selection.
    """
    base = getattr(settings, "autonomy", None)
    default_policy = getattr(base, "default_policy", "balanced")
    override = person.autonomy_override

    if override == AutonomyOverride.never_autonomous:
        return "cautious"
    if override == AutonomyOverride.inherit:
        return default_policy

    notch = _PRESET_NOTCH.get(default_policy, 1)
    if override == AutonomyOverride.more_cautious:
        notch = max(0, notch - 1)
    elif override == AutonomyOverride.more_aggressive:
        notch = min(2, notch + 1)
    return _NOTCH_PRESET[notch]


__all__ = ["PeopleStore", "PersonNotFoundError", "effective_autonomy"]
