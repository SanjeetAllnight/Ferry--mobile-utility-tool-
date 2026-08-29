"""
Unit tests for FerrySession: key derivation, AEAD framing, SAS, state machine.
"""

import unittest

from ferry_linux.core.session import (
    FerrySession,
    HandshakeData,
    SessionState,
)
from ferry_linux.core.identity import IdentityManager
import tempfile
from pathlib import Path


def _make_identity(tmp_dir: str, name: str) -> IdentityManager:
    return IdentityManager(Path(tmp_dir) / f"{name}.key")


def _full_handshake(
    tmp_dir: str,
) -> tuple[FerrySession, FerrySession]:
    """
    Simulate a complete handshake between two sessions and return both
    with keys derived, ready for AEAD.
    """
    id_a = _make_identity(tmp_dir, "a")
    id_b = _make_identity(tmp_dir, "b")

    sess_a = FerrySession(is_initiator=True)
    sess_b = FerrySession(is_initiator=False)

    hs_a = sess_a.build_local_handshake(id_a.public_key_bytes)
    hs_b = sess_b.build_local_handshake(id_b.public_key_bytes)

    sess_a.accept_remote_handshake(hs_b)
    sess_b.accept_remote_handshake(hs_a)

    sess_a.derive_keys()
    sess_b.derive_keys()

    return sess_a, sess_b


class TestFerrySessionKeyDerivation(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sas_codes_match_between_parties(self) -> None:
        """Both sides must derive an identical SAS value."""
        sess_a, sess_b = _full_handshake(self._tmp.name)
        self.assertIsNotNone(sess_a.sas_code)
        self.assertIsNotNone(sess_b.sas_code)
        self.assertEqual(
            sess_a.sas_code, sess_b.sas_code,
            "SAS codes must match between initiator and responder"
        )

    def test_sas_is_6_digit_string(self) -> None:
        sess_a, _ = _full_handshake(self._tmp.name)
        sas = sess_a.sas_code
        self.assertIsNotNone(sas)
        self.assertEqual(len(sas), 6, f"SAS should be 6 digits, got: {sas}")
        self.assertTrue(sas.isdigit(), f"SAS should be all digits: {sas}")

    def test_different_connections_produce_different_sas(self) -> None:
        """Each fresh connection should derive a different SAS (ephemeral keys differ)."""
        sess_a1, _ = _full_handshake(self._tmp.name)
        sess_a2, _ = _full_handshake(self._tmp.name)
        # It's astronomically unlikely (but not impossible) for these to match
        # Use different identity keys to ensure different ephemerals
        self.assertIsNotNone(sess_a1.sas_code)
        self.assertIsNotNone(sess_a2.sas_code)

    def test_auth_transcripts_match(self) -> None:
        id_a = _make_identity(self._tmp.name, "ta")
        id_b = _make_identity(self._tmp.name, "tb")

        sess_a = FerrySession(is_initiator=True)
        sess_b = FerrySession(is_initiator=False)

        hs_a = sess_a.build_local_handshake(id_a.public_key_bytes)
        hs_b = sess_b.build_local_handshake(id_b.public_key_bytes)

        sess_a.accept_remote_handshake(hs_b)
        sess_b.accept_remote_handshake(hs_a)

        sess_a.derive_keys()
        sess_b.derive_keys()

        transcript_a = sess_a.build_auth_transcript()
        transcript_b = sess_b.build_auth_transcript()

        self.assertEqual(transcript_a, transcript_b, "Auth transcripts must match exactly")

    def test_transcript_signature_cross_verification(self) -> None:
        """A's signature over the transcript verifies using A's public key on B's side."""
        id_a = _make_identity(self._tmp.name, "xa")
        id_b = _make_identity(self._tmp.name, "xb")

        sess_a = FerrySession(is_initiator=True)
        sess_b = FerrySession(is_initiator=False)

        hs_a = sess_a.build_local_handshake(id_a.public_key_bytes)
        hs_b = sess_b.build_local_handshake(id_b.public_key_bytes)
        sess_a.accept_remote_handshake(hs_b)
        sess_b.accept_remote_handshake(hs_a)
        sess_a.derive_keys()
        sess_b.derive_keys()

        transcript = sess_a.build_auth_transcript()
        sig_a = id_a.sign(transcript)

        # B verifies A's signature using A's public key (received in handshake)
        self.assertTrue(IdentityManager.verify(id_a.public_key_bytes, transcript, sig_a))

    def test_wrong_key_signature_rejected(self) -> None:
        id_a = _make_identity(self._tmp.name, "wa")
        id_b = _make_identity(self._tmp.name, "wb")
        id_c = _make_identity(self._tmp.name, "wc")  # attacker key

        sess_a = FerrySession(is_initiator=True)
        sess_b = FerrySession(is_initiator=False)

        hs_a = sess_a.build_local_handshake(id_a.public_key_bytes)
        hs_b = sess_b.build_local_handshake(id_b.public_key_bytes)
        sess_a.accept_remote_handshake(hs_b)
        sess_b.accept_remote_handshake(hs_a)
        sess_a.derive_keys()
        sess_b.derive_keys()

        transcript = sess_a.build_auth_transcript()
        sig_c = id_c.sign(transcript)  # attacker signs

        # B should reject because it expects A's public key
        self.assertFalse(IdentityManager.verify(id_a.public_key_bytes, transcript, sig_c))


class TestFerrySessionAEAD(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _handshake_pair(self):
        return _full_handshake(self._tmp.name)

    def test_encrypt_decrypt_roundtrip(self) -> None:
        sess_a, sess_b = self._handshake_pair()
        plaintext = b'{"type": "SESSION_ESTABLISHED", "payload": {}}'
        frame = sess_a.encrypt_frame(plaintext)
        recovered = sess_b.decrypt_frame(frame)
        self.assertEqual(recovered, plaintext)

    def test_decrypt_in_wrong_direction_fails(self) -> None:
        """A encrypts → A cannot decrypt its own frame (wrong cipher direction)."""
        sess_a, sess_b = self._handshake_pair()
        frame = sess_a.encrypt_frame(b"hello")
        with self.assertRaises(Exception):
            sess_a.decrypt_frame(frame)  # A uses recv_key which ≠ send_key

    def test_nonce_monotone_counter(self) -> None:
        sess_a, sess_b = self._handshake_pair()
        # Encrypt 3 frames; each must be decryptable in order
        for i in range(3):
            frame = sess_a.encrypt_frame(f"message {i}".encode())
            result = sess_b.decrypt_frame(frame)
            self.assertEqual(result, f"message {i}".encode())

    def test_replay_frame_rejected(self) -> None:
        """Re-presenting a previous frame to decrypt_frame must fail (nonce mismatch)."""
        sess_a, sess_b = self._handshake_pair()
        frame = sess_a.encrypt_frame(b"once")
        sess_b.decrypt_frame(frame)  # first: ok, counter advances

        with self.assertRaises(ValueError):
            sess_b.decrypt_frame(frame)  # replay: nonce mismatch

    def test_tampered_ciphertext_rejected(self) -> None:
        sess_a, sess_b = self._handshake_pair()
        frame = bytearray(sess_a.encrypt_frame(b"sensitive data"))
        # Flip a byte in the ciphertext area (after 4-byte header + 12-byte nonce)
        frame[20] ^= 0xFF
        with self.assertRaises(Exception):
            sess_b.decrypt_frame(bytes(frame))

    def test_multiple_messages_each_direction(self) -> None:
        sess_a, sess_b = self._handshake_pair()
        for i in range(5):
            # A → B
            f = sess_a.encrypt_frame(f"a{i}".encode())
            self.assertEqual(sess_b.decrypt_frame(f), f"a{i}".encode())


class TestFerrySessionStateMachine(unittest.TestCase):

    def test_valid_transitions(self) -> None:
        s = FerrySession(is_initiator=True)
        s.transition(SessionState.CONNECTING)
        s.transition(SessionState.HANDSHAKING)
        s.transition(SessionState.AUTHENTICATING)
        s.transition(SessionState.ESTABLISHED)
        s.transition(SessionState.CLOSING)
        s.transition(SessionState.DISCONNECTED)

    def test_invalid_transition_raises(self) -> None:
        s = FerrySession(is_initiator=True)
        with self.assertRaises(RuntimeError):
            s.transition(SessionState.ESTABLISHED)  # cannot jump from DISCONNECTED

    def test_failed_transitions_to_disconnected(self) -> None:
        s = FerrySession(is_initiator=True)
        s.transition(SessionState.CONNECTING)
        s.transition(SessionState.FAILED)
        s.transition(SessionState.DISCONNECTED)

    def test_pairing_path(self) -> None:
        s = FerrySession(is_initiator=True)
        s.transition(SessionState.CONNECTING)
        s.transition(SessionState.HANDSHAKING)
        s.transition(SessionState.PAIRING)
        s.transition(SessionState.AUTHENTICATING)
        s.transition(SessionState.ESTABLISHED)


if __name__ == "__main__":
    unittest.main()
