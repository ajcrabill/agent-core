"""IdentityManager — wire Identity row + ed25519 keypair end-to-end.

Per the design: each agent has exactly one Identity row (id='self'). The
public ed25519 key lives ON the row (so peers can fetch it via mesh
endpoint discovery); the seed lives in the secrets store under namespace
``identity``, key ``ed25519_seed_b64``.

This is the surface the wizard uses during ``agent-core init``:
  IdentityManager(db, secrets).bootstrap(
      instance_name="Loriah",
      persona_email="loriahcrabill@gmail.com",
      principal_name="AJ Crabill",
  )

It's also the surface mesh.MeshClient uses to instantiate its signer:
  signer = IdentityManager(db, secrets).get_signer()
"""

from __future__ import annotations

import logging

from agent_core.mesh.signer import Ed25519Signer
from agent_core.secrets import SecretStore
from agent_core.state.db import Database
from agent_core.state.models import Identity, utcnow

logger = logging.getLogger(__name__)


_IDENTITY_NAMESPACE = "identity"
_SEED_KEY = "ed25519_seed_b64"


class IdentityNotInitializedError(RuntimeError):
    """Raised when an operation requires Identity but bootstrap() hasn't run."""


class IdentityManager:
    """High-level identity lifecycle for one agent install.

    Args:
        db: agent-core Database
        secrets: SecretStore (use ``default_store()`` in production;
                 MemorySecretStore for tests)
    """

    def __init__(self, db: Database, secrets: SecretStore) -> None:
        self.db = db
        self.secrets = secrets

    # ── Bootstrap ───────────────────────────────────────────────────────────

    def bootstrap(
        self,
        *,
        instance_name: str,
        persona_email: str | None = None,
        persona_summary: str | None = None,
        principal_name: str | None = None,
        principal_email: str | None = None,
    ) -> Identity:
        """Create the Identity row + generate the ed25519 keypair.

        Idempotent on already-bootstrapped state: if Identity('self')
        already exists with a public key, returns it unchanged unless any
        passed argument differs (in which case those fields are updated).

        The private key (seed) is stored in ``secrets`` under namespace
        'identity', key 'ed25519_seed_b64'. Treat as a secret.
        """
        with self.db.session() as s:
            existing = s.get(Identity, "self")
            if existing is not None:
                # Update mutable fields if provided
                changed = False
                if instance_name and existing.instance_name != instance_name:
                    existing.instance_name = instance_name
                    changed = True
                if persona_email is not None and existing.persona_email != persona_email:
                    existing.persona_email = persona_email
                    changed = True
                if persona_summary is not None and existing.persona_summary != persona_summary:
                    existing.persona_summary = persona_summary
                    changed = True
                if principal_name is not None and existing.principal_name != principal_name:
                    existing.principal_name = principal_name
                    changed = True
                if principal_email is not None and existing.principal_email != principal_email:
                    existing.principal_email = principal_email
                    changed = True
                if changed:
                    existing.updated_at = utcnow()
                    s.add(existing)
                    s.commit()
                    s.refresh(existing)
                    logger.info("identity updated for %s", existing.instance_name)
                # If no public key yet (legacy install), generate one
                if not existing.public_key:
                    self._generate_and_store_keypair(existing)
                return existing

            # Fresh install — create row + keypair
            ident = Identity(
                instance_name=instance_name,
                persona_email=persona_email,
                persona_summary=persona_summary,
                principal_name=principal_name,
                principal_email=principal_email,
            )
            s.add(ident)
            s.commit()
            s.refresh(ident)

        self._generate_and_store_keypair(ident)
        logger.info(
            "identity bootstrapped: instance=%s pubkey=%s",
            instance_name,
            ident.public_key[:16] + "…" if ident.public_key else "(none)",
        )
        return ident

    def _generate_and_store_keypair(self, ident: Identity) -> None:
        """Generate ed25519 keypair, save seed in secrets, save public key
        on the Identity row."""
        signer = Ed25519Signer.generate()
        self.secrets.set(_IDENTITY_NAMESPACE, _SEED_KEY, signer.seed_b64)
        with self.db.session() as s:
            row = s.get(Identity, "self")
            row.public_key = signer.public_key_b64
            row.updated_at = utcnow()
            s.add(row)
            s.commit()
            ident.public_key = row.public_key

    # ── Read API ────────────────────────────────────────────────────────────

    def get_identity(self) -> Identity:
        """Return the Identity row. Raises if not bootstrapped."""
        with self.db.session() as s:
            ident = s.get(Identity, "self")
        if ident is None:
            raise IdentityNotInitializedError(
                "Identity not bootstrapped — call IdentityManager.bootstrap(...) first"
            )
        return ident

    def get_instance_name(self) -> str:
        return self.get_identity().instance_name

    def get_public_key(self) -> str:
        ident = self.get_identity()
        if not ident.public_key:
            raise IdentityNotInitializedError(
                "Identity has no public key — keypair never generated"
            )
        return ident.public_key

    def get_signer(self) -> Ed25519Signer:
        """Construct an Ed25519Signer from the private seed in secrets.

        Raises IdentityNotInitializedError if no seed is stored.
        """
        seed = self.secrets.get(_IDENTITY_NAMESPACE, _SEED_KEY)
        if not seed:
            raise IdentityNotInitializedError(
                "no ed25519 seed in secrets (namespace 'identity', key "
                "'ed25519_seed_b64') — run bootstrap() to generate one"
            )
        return Ed25519Signer(seed)

    # ── Mutation ────────────────────────────────────────────────────────────

    def update_persona(
        self,
        *,
        instance_name: str | None = None,
        persona_email: str | None = None,
        persona_summary: str | None = None,
    ) -> Identity:
        """Update persona-side fields. Pass only what's changing."""
        self.get_identity()
        changed = False
        with self.db.session() as s:
            row = s.get(Identity, "self")
            if instance_name is not None and row.instance_name != instance_name:
                row.instance_name = instance_name
                changed = True
            if persona_email is not None and row.persona_email != persona_email:
                row.persona_email = persona_email
                changed = True
            if persona_summary is not None and row.persona_summary != persona_summary:
                row.persona_summary = persona_summary
                changed = True
            if changed:
                row.updated_at = utcnow()
                s.add(row)
                s.commit()
                s.refresh(row)
            return row

    def update_principal(
        self,
        *,
        principal_name: str | None = None,
        principal_email: str | None = None,
    ) -> Identity:
        """Update principal-side fields. Pass only what's changing."""
        self.get_identity()
        with self.db.session() as s:
            row = s.get(Identity, "self")
            changed = False
            if principal_name is not None and row.principal_name != principal_name:
                row.principal_name = principal_name
                changed = True
            if principal_email is not None and row.principal_email != principal_email:
                row.principal_email = principal_email
                changed = True
            if changed:
                row.updated_at = utcnow()
                s.add(row)
                s.commit()
                s.refresh(row)
            return row

    # ── Reset (use with care) ───────────────────────────────────────────────

    def regenerate_keypair(self) -> Identity:
        """Generate a fresh keypair, replacing the existing one in both the
        secrets store and on the Identity row.

        WARNING: peers won't know about the new key until you re-share it.
        Use this only when you know the old key has been compromised or
        you're moving to a new install.
        """
        ident = self.get_identity()
        self._generate_and_store_keypair(ident)
        return self.get_identity()


__all__ = ["IdentityManager", "IdentityNotInitializedError"]
