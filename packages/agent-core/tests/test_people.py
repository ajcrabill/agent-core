"""Tests for agent_core.people.PeopleStore + Person model + autonomy resolution."""

from __future__ import annotations

import pytest
from agent_core.people import (
    PeopleStore,
    PersonNotFoundError,
    effective_autonomy,
)
from agent_core.settings import AgentSettings
from agent_core.state import AutonomyOverride, Database, Person

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db() -> Database:
    d = Database.sqlite_memory()
    d.create_all()
    return d


@pytest.fixture
def store(db: Database) -> PeopleStore:
    return PeopleStore(db)


# ── Schema sanity ──────────────────────────────────────────────────────────


def test_person_model_has_expected_fields() -> None:
    """Ensure the model exposes the field set lifted from Esby's schema."""
    p = Person(name="Test")
    for attr in (
        "id",
        "name",
        "organization",
        "role",
        "stakeholder_class",
        "autonomy_override",
        "relationship_intensity",
        "response_sla",
        "never_autonomous_send",
        "sensitive_memory_flag",
        "contact_methods",
        "notes_path",
        "metadata_json",
        "created_at",
        "updated_at",
    ):
        assert hasattr(p, attr), f"Person missing field {attr!r}"


def test_person_defaults() -> None:
    p = Person(name="Test")
    assert p.stakeholder_class == "unknown_external"
    assert p.autonomy_override == AutonomyOverride.inherit
    assert p.never_autonomous_send is False
    assert p.sensitive_memory_flag is False
    assert p.contact_methods == {}


# ── Upsert / create / update ───────────────────────────────────────────────


def test_upsert_creates_new_person(store: PeopleStore) -> None:
    p = store.upsert(name="Charlotte Grinberg", relationship_intensity=3)
    assert p.id
    assert p.name == "Charlotte Grinberg"
    assert p.relationship_intensity == 3
    assert store.count() == 1


def test_upsert_updates_existing_by_name_and_org(store: PeopleStore) -> None:
    a = store.upsert(name="Greg", organization="ESB")
    b = store.upsert(name="Greg", organization="ESB", role="director")
    assert a.id == b.id
    assert b.role == "director"
    assert store.count() == 1


def test_upsert_creates_separate_rows_for_same_name_diff_orgs(store: PeopleStore) -> None:
    a = store.upsert(name="Sam", organization="OrgA")
    b = store.upsert(name="Sam", organization="OrgB")
    assert a.id != b.id
    assert store.count() == 2


def test_upsert_strips_whitespace_and_is_case_insensitive(store: PeopleStore) -> None:
    a = store.upsert(name="  Robyne  ")
    b = store.upsert(name="ROBYNE")
    assert a.id == b.id


def test_upsert_requires_non_empty_name(store: PeopleStore) -> None:
    with pytest.raises(ValueError):
        store.upsert(name="")
    with pytest.raises(ValueError):
        store.upsert(name="   ")


def test_upsert_persists_contact_methods(store: PeopleStore) -> None:
    p = store.upsert(
        name="Charlotte",
        contact_methods={"email": "charlotte@example.com", "sms": "+15555551234"},
    )
    fetched = store.get(p.id)
    assert fetched.contact_methods["email"] == "charlotte@example.com"
    assert fetched.contact_methods["sms"] == "+15555551234"


# ── Lookups ────────────────────────────────────────────────────────────────


def test_get_returns_none_for_missing(store: PeopleStore) -> None:
    assert store.get("does-not-exist") is None


def test_require_raises_for_missing(store: PeopleStore) -> None:
    with pytest.raises(PersonNotFoundError):
        store.require("does-not-exist")


def test_find_by_name_case_insensitive(store: PeopleStore) -> None:
    store.upsert(name="Charlotte Grinberg")
    matches = store.find_by_name("charlotte grinberg")
    assert len(matches) == 1


def test_find_by_name_returns_multiple_when_same_name_diff_orgs(store: PeopleStore) -> None:
    store.upsert(name="Sam", organization="A")
    store.upsert(name="Sam", organization="B")
    matches = store.find_by_name("Sam")
    assert len(matches) == 2


def test_find_by_email_returns_person(store: PeopleStore) -> None:
    p = store.upsert(
        name="Charlotte",
        contact_methods={"email": "charlotte@example.com"},
    )
    found = store.find_by_email("CHARLOTTE@example.com")
    assert found is not None
    assert found.id == p.id


def test_find_by_email_handles_list_of_addresses(store: PeopleStore) -> None:
    store.upsert(
        name="Sam",
        contact_methods={"email": ["sam@a.com", "sam@b.com"]},
    )
    assert store.find_by_email("sam@b.com") is not None


def test_find_by_email_returns_none_when_no_match(store: PeopleStore) -> None:
    store.upsert(name="No Email")
    assert store.find_by_email("nobody@nowhere.com") is None


def test_find_by_email_handles_empty_input(store: PeopleStore) -> None:
    store.upsert(name="X", contact_methods={"email": "x@y.com"})
    assert store.find_by_email("") is None
    assert store.find_by_email("   ") is None


def test_list_filters_by_stakeholder_class(store: PeopleStore) -> None:
    store.upsert(name="A", stakeholder_class="family_member")
    store.upsert(name="B", stakeholder_class="principal_client")
    store.upsert(name="C", stakeholder_class="family_member")
    assert len(store.list(stakeholder_class="family_member")) == 2
    assert len(store.list(stakeholder_class="principal_client")) == 1


def test_list_filters_by_never_autonomous_send(store: PeopleStore) -> None:
    store.upsert(name="A", never_autonomous_send=True)
    store.upsert(name="B", never_autonomous_send=False)
    locked = store.list(never_autonomous_send=True)
    assert len(locked) == 1
    assert locked[0].name == "A"


def test_list_orders_by_name(store: PeopleStore) -> None:
    store.upsert(name="Charlie")
    store.upsert(name="Alice")
    store.upsert(name="Bob")
    names = [p.name for p in store.list()]
    assert names == ["Alice", "Bob", "Charlie"]


# ── Effective autonomy resolution ──────────────────────────────────────────


def test_effective_autonomy_inherit_returns_default() -> None:
    p = Person(name="x", autonomy_override=AutonomyOverride.inherit)
    s = AgentSettings(autonomy={"default_policy": "balanced"})  # type: ignore[arg-type]
    assert effective_autonomy(p, s) == "balanced"


def test_effective_autonomy_never_autonomous_forces_cautious() -> None:
    """Hard floor — 'never_autonomous' returns cautious even on aggressive presets."""
    p = Person(name="x", autonomy_override=AutonomyOverride.never_autonomous)
    s = AgentSettings(autonomy={"default_policy": "aggressive"})  # type: ignore[arg-type]
    assert effective_autonomy(p, s) == "cautious"


def test_effective_autonomy_more_cautious_shifts_one_notch() -> None:
    p = Person(name="x", autonomy_override=AutonomyOverride.more_cautious)
    for default, expected in (
        ("aggressive", "balanced"),
        ("balanced", "cautious"),
        ("cautious", "cautious"),  # clamps at floor
    ):
        s = AgentSettings(autonomy={"default_policy": default})  # type: ignore[arg-type]
        assert effective_autonomy(p, s) == expected, default


def test_effective_autonomy_more_aggressive_shifts_one_notch() -> None:
    p = Person(name="x", autonomy_override=AutonomyOverride.more_aggressive)
    for default, expected in (
        ("cautious", "balanced"),
        ("balanced", "aggressive"),
        ("aggressive", "aggressive"),  # clamps at ceiling
    ):
        s = AgentSettings(autonomy={"default_policy": default})  # type: ignore[arg-type]
        assert effective_autonomy(p, s) == expected, default


# ── Autonomy override: round-trip through DB ───────────────────────────────


def test_autonomy_override_persists(store: PeopleStore) -> None:
    p = store.upsert(name="X", autonomy_override=AutonomyOverride.never_autonomous)
    fetched = store.get(p.id)
    assert fetched.autonomy_override == AutonomyOverride.never_autonomous


def test_filter_by_autonomy_override(store: PeopleStore) -> None:
    store.upsert(name="A", autonomy_override=AutonomyOverride.never_autonomous)
    store.upsert(name="B", autonomy_override=AutonomyOverride.inherit)
    store.upsert(name="C", autonomy_override=AutonomyOverride.never_autonomous)
    locked = store.list(autonomy_override=AutonomyOverride.never_autonomous)
    assert {p.name for p in locked} == {"A", "C"}
