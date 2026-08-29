"""
Ferry Ed25519 Identity Key Management.

Manages the persistent long-term Ed25519 identity keypair for this device.
The private key NEVER leaves this module and is NEVER transmitted over the network.
The public key (32 bytes, base64url encoded) is safe to share and is advertised
during handshake as proof of cryptographic identity.

Key storage: $XDG_DATA_HOME/ferry/identity.key (PEM, permissions 0600)
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger("ferry.identity")

# Private key file permissions: owner read+write only
_KEY_FILE_MODE = 0o600


class IdentityManager:
    """
    Manages the local Ferry Ed25519 device identity keypair.

    Thread-safety: All methods are safe to call from multiple threads;
    the key is loaded once at construction and is immutable thereafter.
    """

    def __init__(self, key_file: Path) -> None:
        self._key_file = key_file
        self._private_key: Ed25519PrivateKey = self._load_or_generate()
        self._public_key: Ed25519PublicKey = self._private_key.public_key()
        self._public_key_bytes: bytes = self._public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        logger.info(
            "Ferry identity loaded. Public key: %s",
            self.public_key_b64[:12] + "...",
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte Ed25519 public key."""
        return self._public_key_bytes

    @property
    def public_key_b64(self) -> str:
        """URL-safe base64 encoded public key (no padding)."""
        return base64.urlsafe_b64encode(self._public_key_bytes).rstrip(b"=").decode()

    def sign(self, message: bytes) -> bytes:
        """
        Sign arbitrary bytes with the local Ed25519 private key.
        Returns 64-byte raw signature.
        The private key material is NEVER returned or logged.
        """
        return self._private_key.sign(message)

    @staticmethod
    def verify(public_key_bytes: bytes, message: bytes, signature: bytes) -> bool:
        """
        Verify an Ed25519 signature from a remote peer.

        Args:
            public_key_bytes: Raw 32-byte public key of the signer.
            message: Message that was signed.
            signature: 64-byte raw Ed25519 signature.

        Returns:
            True if valid, False if invalid.
        """
        try:
            pub = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            pub.verify(signature, message)
            return True
        except (InvalidSignature, ValueError, Exception):
            return False

    @staticmethod
    def decode_public_key_b64(b64_str: str) -> bytes:
        """Decode a base64url (no padding) encoded public key to raw bytes."""
        padding = 4 - (len(b64_str) % 4)
        if padding != 4:
            b64_str += "=" * padding
        return base64.urlsafe_b64decode(b64_str)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_or_generate(self) -> Ed25519PrivateKey:
        """Load existing key from disk or generate and persist a new one."""
        if self._key_file.exists():
            try:
                return self._load_key()
            except Exception as exc:
                logger.warning("Failed to load identity key (%s); generating new key.", exc)

        return self._generate_and_save()

    def _load_key(self) -> Ed25519PrivateKey:
        """Read PEM-encoded private key from disk."""
        with open(self._key_file, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("Stored key is not an Ed25519 private key")
        return key

    def _generate_and_save(self) -> Ed25519PrivateKey:
        """Generate a fresh Ed25519 private key and persist it securely."""
        key = Ed25519PrivateKey.generate()
        self._key_file.parent.mkdir(parents=True, exist_ok=True)

        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

        # Write with restrictive permissions
        fd = os.open(self._key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KEY_FILE_MODE)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)

        # Enforce 0600 in case umask was permissive
        os.chmod(self._key_file, _KEY_FILE_MODE)
        logger.info("Generated new Ed25519 identity key at %s", self._key_file)
        return key
