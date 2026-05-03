"""Sprint 7a — IdentityManager tests."""

from __future__ import annotations

import pytest
from agent_core.identity import IdentityManager, IdentityNotInitializedError
from agent_core.mesh.types import MessageEnvelope
from agent_core.secrets import MemorySecretStore
from agent_core.state import Database, Identity


def _empty_db() -> Database:
    db = Database.sqlite_memory()
    db.create_all()
    return db


def _manager() -> IdentityManager:
    return IdentityManager(_empty_db(), MemorySecretStore())


# ── Bootstrap ───────────────────────────────────────────────────────────────


def test_bootstrap_creates_identity_row() -> None:
    mgr = _manager()
    ident = mgr.bootstrap(
        instance_name="Loriah",
        persona_email="loriahcrabill@gmail.com",
        principal_name="AJ Crabill",
        principal_email="ajc@example.com",
    )
    assert ident.instance_name == "Loriah"
    assert ident.persona_email == "loriahcrabill@gmail.com"
    assert ident.principal_name == "AJ Crabill"
    assert ident.principal_email == "ajc@example.com"


def test_bootstrap_generates_keypair_and_stores_seed() -> None:
    db = _empty_db()
    secrets = MemorySecretStore()
    mgr = IdentityManager(db, secrets)
    ident = mgr.bootstrap(instance_name="Loriah")
    # Public key on row
    assert ident.public_key
    # Seed in secrets
    seed = secrets.get("identity", "ed25519_seed_b64")
    assert seed is not None
    # And signer is recoverable
    signer = mgr.get_signer()
    assert signer.public_key_b64 == ident.public_key


def test_bootstrap_idempotent_on_repeat() -> None:
    """Calling bootstrap a second time with same args returns the same
    identity (doesn't regenerate the keypair)."""
    db = _empty_db()
    secrets = MemorySecretStore()
    mgr = IdentityManager(db, secrets)
    ident1 = mgr.bootstrap(instance_name="Loriah")
    pk1 = ident1.public_key
    seed1 = secrets.get("identity", "ed25519_seed_b64")

    ident2 = mgr.bootstrap(instance_name="Loriah")
    assert ident2.public_key == pk1
    assert secrets.get("identity", "ed25519_seed_b64") == seed1


def test_bootstrap_updates_changed_persona_fields() -> None:
    """Re-bootstrapping with a new persona_email updates that field."""
    mgr = _manager()
    mgr.bootstrap(instance_name="Loriah", persona_email="old@x.com")
    ident = mgr.bootstrap(instance_name="Loriah", persona_email="new@x.com")
    assert ident.persona_email == "new@x.com"


def test_bootstrap_regenerates_keypair_for_legacy_keyless_identity() -> None:
    """If an Identity row exists without a public key (legacy install),
    bootstrap() generates one rather than leaving it broken."""
    db = _empty_db()
    secrets = MemorySecretStore()
    with db.session() as s:
        s.add(Identity(instance_name="LegacyAgent"))
        s.commit()
    ident = IdentityManager(db, secrets).bootstrap(instance_name="LegacyAgent")
    assert ident.public_key
    assert secrets.get("identity", "ed25519_seed_b64") is not None


# ── Read API ────────────────────────────────────────────────────────────────


def test_get_identity_raises_when_not_bootstrapped() -> None:
    mgr = _manager()
    with pytest.raises(IdentityNotInitializedError):
        mgr.get_identity()


def test_get_instance_name_after_bootstrap() -> None:
    mgr = _manager()
    mgr.bootstrap(instance_name="Loriah")
    assert mgr.get_instance_name() == "Loriah"


def test_get_public_key_returns_b64_string() -> None:
    mgr = _manager()
    mgr.bootstrap(instance_name="Loriah")
    pk = mgr.get_public_key()
    import base64

    raw = base64.b64decode(pk)
    assert len(raw) == 32  # ed25519 public key


def test_get_signer_round_trips_to_envelope_signature() -> None:
    """The signer returned by get_signer() can sign + verify against the
    Identity's public key."""
    mgr = _manager()
    mgr.bootstrap(instance_name="Loriah")
    signer = mgr.get_signer()
    env = MessageEnvelope(id="x", sender="Loriah", recipient="Esby", body="hi")
    env.signature = signer.sign(env)
    signer.verify(env, mgr.get_public_key())  # no exception


def test_get_signer_raises_without_seed() -> None:
    """Edge case: Identity row exists but seed missing (e.g., partial
    install). get_signer() raises with a helpful message."""
    db = _empty_db()
    secrets = MemorySecretStore()
    with db.session() as s:
        s.add(Identity(instance_name="X", public_key="abc"))
        s.commit()
    with pytest.raises(IdentityNotInitializedError, match="ed25519 seed"):
        IdentityManager(db, secrets).get_signer()


# ── Update ──────────────────────────────────────────────────────────────────


def test_update_persona_changes_only_passed_fields() -> None:
    mgr = _manager()
    mgr.bootstrap(
        instance_name="Loriah",
        persona_email="orig@x.com",
        persona_summary="orig summary",
    )
    updated = mgr.update_persona(persona_email="new@x.com")
    assert updated.persona_email == "new@x.com"
    assert updated.persona_summary == "orig summary"  # untouched


def test_update_principal_changes_only_passed_fields() -> None:
    mgr = _manager()
    mgr.bootstrap(
        instance_name="Loriah",
        principal_name="AJ",
        principal_email="ajc@x.com",
    )
    updated = mgr.update_principal(principal_name="AJ Crabill")
    assert updated.principal_name == "AJ Crabill"
    assert updated.principal_email == "ajc@x.com"


# ── Regenerate ──────────────────────────────────────────────────────────────


def test_regenerate_keypair_replaces_both_seed_and_public_key() -> None:
    db = _empty_db()
    secrets = MemorySecretStore()
    mgr = IdentityManager(db, secrets)
    mgr.bootstrap(instance_name="Loriah")
    old_pk = mgr.get_public_key()
    old_seed = secrets.get("identity", "ed25519_seed_b64")

    mgr.regenerate_keypair()
    new_pk = mgr.get_public_key()
    new_seed = secrets.get("identity", "ed25519_seed_b64")
    assert new_pk != old_pk
    assert new_seed != old_seed


def test_regenerate_signer_produces_signatures_that_verify_against_new_key() -> None:
    mgr = _manager()
    mgr.bootstrap(instance_name="Loriah")
    mgr.regenerate_keypair()
    signer = mgr.get_signer()
    env = MessageEnvelope(id="x", sender="Loriah", recipient="Esby", body="hi")
    env.signature = signer.sign(env)
    signer.verify(env, mgr.get_public_key())


# ── End-to-end: identity + mesh ────────────────────────────────────────────


def test_identity_signer_works_with_mesh_client() -> None:
    """The signer produced by IdentityManager can be plugged straight into
    MeshClient. End-to-end smoke."""
    from agent_core.mesh import (
        InProcessTransport,
        MeshClient,
        PeerRegistry,
    )

    db = _empty_db()
    secrets = MemorySecretStore()
    mgr = IdentityManager(db, secrets)
    mgr.bootstrap(instance_name="Loriah")

    pr = PeerRegistry(db)
    transport = InProcessTransport()
    client = MeshClient(db, signer=mgr.get_signer(), transport=transport, peers=pr)
    # The client knows who it is via Identity
    assert client.instance_name == "Loriah"
