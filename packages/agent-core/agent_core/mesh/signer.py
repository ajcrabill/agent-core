"""Ed25519 signer — sign outbound messages, verify inbound ones.

PyNaCl is already in the agent-core deps. Each agent has an ed25519 keypair;
the public key is stored on the Identity row (and shared with peers); the
private key lives in the secrets store (Sprint 7 — for now, the signer
accepts the seed bytes directly).

Wire format: signature is a base64-encoded 64-byte ed25519 signature over
``MessageEnvelope.signed_payload_bytes()``.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

from agent_core.mesh.types import MessageEnvelope, SignatureError


@runtime_checkable
class Signer(Protocol):
    """Sign and verify mesh messages."""

    def sign(self, envelope: MessageEnvelope) -> str:
        """Return base64 signature for ``envelope``."""

    def verify(
        self,
        envelope: MessageEnvelope,
        sender_public_key: str,
    ) -> None:
        """Verify ``envelope.signature`` against ``sender_public_key``.

        Raises SignatureError on mismatch.
        """


# ── Ed25519 (production) ─────────────────────────────────────────────────────


class Ed25519Signer:
    """Real ed25519 signer using PyNaCl. Provide either a `seed` (32 bytes,
    base64 or raw) for the private key, or use ``generate()`` to create one."""

    def __init__(self, seed: bytes | str) -> None:
        from nacl.signing import SigningKey

        if isinstance(seed, str):
            seed = base64.b64decode(seed)
        if len(seed) != 32:
            raise ValueError(f"ed25519 seed must be 32 bytes, got {len(seed)}")
        self._signing_key = SigningKey(seed)

    @classmethod
    def generate(cls) -> Ed25519Signer:
        """Create a signer with a freshly-generated keypair."""
        from nacl.signing import SigningKey

        sk = SigningKey.generate()
        inst = cls.__new__(cls)
        inst._signing_key = sk
        return inst

    @property
    def public_key_b64(self) -> str:
        """Base64-encoded 32-byte public key (share via Peer rows)."""
        return base64.b64encode(bytes(self._signing_key.verify_key)).decode("ascii")

    @property
    def seed_b64(self) -> str:
        """Base64-encoded 32-byte seed. Treat as a secret."""
        return base64.b64encode(bytes(self._signing_key)).decode("ascii")

    # ── Signer Protocol ────────────────────────────────────────────────────

    def sign(self, envelope: MessageEnvelope) -> str:
        signed = self._signing_key.sign(envelope.signed_payload_bytes())
        return base64.b64encode(signed.signature).decode("ascii")

    def verify(
        self,
        envelope: MessageEnvelope,
        sender_public_key: str,
    ) -> None:
        from nacl.signing import VerifyKey

        if envelope.signature is None:
            raise SignatureError("envelope has no signature")
        # Catch every NaCl-raised condition uniformly: bad signature, wrong-
        # length signature, malformed b64, malformed key — all surface as
        # SignatureError so callers don't have to catch a moving target.
        try:
            verify_key = VerifyKey(base64.b64decode(sender_public_key))
            verify_key.verify(
                envelope.signed_payload_bytes(),
                base64.b64decode(envelope.signature),
            )
        except Exception as e:
            raise SignatureError(f"signature verification failed: {e}") from e


# ── Null signer (for tests / development without keys) ──────────────────────


class NullSigner:
    """No-op signer that produces empty signatures and accepts any.

    Useful for unit tests of higher-level mesh logic that don't care about
    crypto. **Never use in production** — it makes signature verification
    meaningless.
    """

    public_key_b64 = ""

    def sign(self, envelope: MessageEnvelope) -> str:
        return ""

    def verify(
        self,
        envelope: MessageEnvelope,
        sender_public_key: str,
    ) -> None:
        return  # accept everything


__all__ = ["Ed25519Signer", "NullSigner", "Signer"]
