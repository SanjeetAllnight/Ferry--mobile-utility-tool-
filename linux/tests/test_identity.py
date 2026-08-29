"""
Unit tests for Ferry Ed25519 IdentityManager.
"""

import base64
import os
import tempfile
import unittest
from pathlib import Path

from ferry_linux.core.identity import IdentityManager


class TestIdentityManager(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._key_file = Path(self._tmp.name) / "identity.key"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_manager(self) -> IdentityManager:
        return IdentityManager(self._key_file)

    # ------------------------------------------------------------------
    # Key generation and persistence
    # ------------------------------------------------------------------

    def test_generates_key_on_first_run(self) -> None:
        mgr = self._make_manager()
        self.assertTrue(self._key_file.exists(), "Key file should be created")
        self.assertEqual(len(mgr.public_key_bytes), 32, "Ed25519 public key must be 32 bytes")

    def test_key_file_permissions_restrictive(self) -> None:
        self._make_manager()
        stat = os.stat(self._key_file)
        mode = stat.st_mode & 0o777
        self.assertEqual(mode, 0o600, f"Key file permissions should be 0600, got {oct(mode)}")

    def test_same_key_loaded_on_second_run(self) -> None:
        mgr1 = self._make_manager()
        pub1 = mgr1.public_key_bytes

        mgr2 = self._make_manager()
        pub2 = mgr2.public_key_bytes

        self.assertEqual(pub1, pub2, "Should reload same key from disk")

    def test_public_key_b64_is_valid_base64url(self) -> None:
        mgr = self._make_manager()
        b64 = mgr.public_key_b64
        # Should be decodable
        decoded = IdentityManager.decode_public_key_b64(b64)
        self.assertEqual(decoded, mgr.public_key_bytes)

    # ------------------------------------------------------------------
    # Signing and verification
    # ------------------------------------------------------------------

    def test_sign_and_verify_roundtrip(self) -> None:
        mgr = self._make_manager()
        message = b"ferry-handshake-transcript-test"
        sig = mgr.sign(message)

        self.assertEqual(len(sig), 64, "Ed25519 signature must be 64 bytes")
        self.assertTrue(
            IdentityManager.verify(mgr.public_key_bytes, message, sig),
            "Signature should verify against own public key"
        )

    def test_verification_fails_wrong_message(self) -> None:
        mgr = self._make_manager()
        sig = mgr.sign(b"correct message")
        self.assertFalse(
            IdentityManager.verify(mgr.public_key_bytes, b"wrong message", sig)
        )

    def test_verification_fails_wrong_key(self) -> None:
        mgr1 = self._make_manager()
        other_key_file = Path(self._tmp.name) / "other.key"
        mgr2 = IdentityManager(other_key_file)

        msg = b"test"
        sig = mgr1.sign(msg)
        self.assertFalse(IdentityManager.verify(mgr2.public_key_bytes, msg, sig))

    def test_verification_fails_tampered_signature(self) -> None:
        mgr = self._make_manager()
        sig = bytearray(mgr.sign(b"message"))
        sig[0] ^= 0xFF  # flip bits
        self.assertFalse(IdentityManager.verify(mgr.public_key_bytes, b"message", bytes(sig)))

    def test_private_key_not_in_public_key_b64(self) -> None:
        """Public key string must not contain private key material."""
        mgr = self._make_manager()
        # PEM private key contains "PRIVATE KEY" text; public_key_b64 must not
        self.assertNotIn("PRIVATE", mgr.public_key_b64)
        self.assertNotIn("KEY", mgr.public_key_b64)

    # ------------------------------------------------------------------
    # Decode helper
    # ------------------------------------------------------------------

    def test_decode_public_key_b64_roundtrip(self) -> None:
        mgr = self._make_manager()
        encoded = mgr.public_key_b64
        decoded = IdentityManager.decode_public_key_b64(encoded)
        self.assertEqual(decoded, mgr.public_key_bytes)

    def test_two_managers_have_different_keys(self) -> None:
        mgr1 = IdentityManager(Path(self._tmp.name) / "k1.key")
        mgr2 = IdentityManager(Path(self._tmp.name) / "k2.key")
        self.assertNotEqual(mgr1.public_key_bytes, mgr2.public_key_bytes)


if __name__ == "__main__":
    unittest.main()
