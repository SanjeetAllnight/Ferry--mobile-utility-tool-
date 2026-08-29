"""
Ferry Secure Session: Handshake, Key Derivation, AEAD Framing, State Machine.

Implements the Ferry Phase 2B secure control-plane protocol:

1. HANDSHAKE_INIT / HANDSHAKE_RESPONSE exchange (plaintext, over TCP)
   - Each side sends static Ed25519 public key + ephemeral X25519 public key + random nonce
2. Both sides independently derive:
   - Shared DH secret via X25519
   - Session keys (send/receive ChaCha20-Poly1305 keys) via HKDF-SHA256
   - SAS verification code (3 bytes → 6-digit number) via HKDF-SHA256
3. First connection (pairing):
   - Both sides display SAS; user verifies match out-of-band
   - AUTH_CHALLENGE / AUTH_RESPONSE carry Ed25519 signatures over the full transcript
4. Subsequent connections (already trusted):
   - AUTH_CHALLENGE / AUTH_RESPONSE, signatures verified against stored public key
5. All messages after handshake: AEAD frames (ChaCha20-Poly1305)

Security properties:
- Forward secrecy: ephemeral X25519 keys discarded after session
- Mutual authentication: Ed25519 signature proof from both sides
- Replay protection: 96-bit monotone nonce per AEAD frame
- No private key material ever transmitted or logged

CRITICAL: Do NOT add logging of private key material anywhere in this file.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes as crypto_hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# AEAD frame constants
AEAD_NONCE_SIZE = 12       # 96 bits for ChaCha20-Poly1305
AEAD_TAG_SIZE = 16         # 128-bit Poly1305 tag
AEAD_HEADER_SIZE = 4       # 32-bit big-endian ciphertext length
MAX_AEAD_PLAINTEXT = 1048576  # 1 MiB

# SAS
SAS_HKDF_INFO = b"ferry-sas-v1"
SAS_BYTES = 3              # → 6-digit decimal number

# Session key derivation
SESSION_KEY_INFO_INITIATOR = b"ferry-session-initiator-v1"
SESSION_KEY_INFO_RESPONDER = b"ferry-session-responder-v1"
SESSION_KEY_LEN = 32       # 256 bits for ChaCha20-Poly1305


class SessionState(Enum):
    """Explicit session state machine states."""
    DISCONNECTED = auto()
    CONNECTING = auto()
    HANDSHAKING = auto()
    PAIRING = auto()           # First-time: awaiting SAS user confirmation
    WAITING_FOR_LOCAL_DECISION = auto()
    WAITING_FOR_REMOTE_DECISION = auto()
    PAIR_ACCEPTED = auto()
    AUTHENTICATING = auto()   # Known peer: verifying Ed25519 signatures
    ESTABLISHED = auto()
    CLOSING = auto()
    FAILED = auto()


@dataclass
class HandshakeData:
    """Data from one side of the handshake. Never contains private key material."""
    static_pub_key: bytes     # 32-byte Ed25519 public key
    ephemeral_pub_key: bytes  # 32-byte X25519 public key
    nonce: bytes              # 16-byte random nonce


@dataclass
class DerivedKeys:
    """Keys derived after X25519 DH + HKDF. Both are symmetric AEAD keys."""
    send_key: bytes   # ChaCha20-Poly1305 key for encrypting outgoing frames
    recv_key: bytes   # ChaCha20-Poly1305 key for decrypting incoming frames
    sas_bytes: bytes  # 3-byte SAS raw value


class FerrySession:
    """
    Manages one secure session between two Ferry devices.

    Typical lifecycle:
        session = FerrySession(is_initiator=True)
        init_msg = session.get_handshake_init(local_identity)
        # send init_msg ...
        # receive remote HandshakeData ...
        session.process_handshake_response(remote_data, local_identity)
        keys = session.derive_keys(is_initiator=True)
        sas = session.get_sas_display()
        # user confirms SAS ...
        transcript = session.build_auth_transcript()
        auth_sig = local_identity.sign(transcript)
        # send auth_sig, verify remote auth_sig ...
        session.transition(SessionState.ESTABLISHED)
    """

    def __init__(self, is_initiator: bool) -> None:
        self._is_initiator = is_initiator
        self._state = SessionState.DISCONNECTED

        # Ephemeral X25519 keypair — discarded after key derivation
        self._eph_private: Optional[X25519PrivateKey] = None
        self._eph_pub_bytes: Optional[bytes] = None

        # Handshake data from both sides
        self._local_hs: Optional[HandshakeData] = None
        self._remote_hs: Optional[HandshakeData] = None

        # Derived symmetric keys
        self._derived: Optional[DerivedKeys] = None

        # AEAD cipher instances
        self._send_cipher: Optional[ChaCha20Poly1305] = None
        self._recv_cipher: Optional[ChaCha20Poly1305] = None

        # Nonce counters (monotone, 12-byte big-endian)
        self._send_counter: int = 0
        self._recv_counter: int = 0

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def sas_code(self) -> Optional[str]:
        """6-digit SAS verification code, or None if keys not yet derived."""
        if self._derived is None:
            return None
        val = int.from_bytes(self._derived.sas_bytes, "big") % 1_000_000
        return f"{val:06d}"

    # ------------------------------------------------------------------
    # Handshake phase (plaintext)
    # ------------------------------------------------------------------

    def generate_ephemeral_keypair(self) -> None:
        """Generate fresh ephemeral X25519 keypair. Call before handshake."""
        self._eph_private = X25519PrivateKey.generate()
        self._eph_pub_bytes = self._eph_private.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )

    def build_local_handshake(self, static_pub_key_bytes: bytes) -> HandshakeData:
        """
        Build the local HandshakeData struct for transmission.

        Args:
            static_pub_key_bytes: Raw 32-byte Ed25519 public key of this device.
        """
        if self._eph_pub_bytes is None:
            self.generate_ephemeral_keypair()

        local_nonce = os.urandom(16)
        self._local_hs = HandshakeData(
            static_pub_key=static_pub_key_bytes,
            ephemeral_pub_key=self._eph_pub_bytes,  # type: ignore[arg-type]
            nonce=local_nonce,
        )
        return self._local_hs

    def accept_remote_handshake(self, remote: HandshakeData) -> None:
        """Store the peer's handshake data received over the wire."""
        self._remote_hs = remote

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    def derive_keys(self) -> DerivedKeys:
        """
        Perform X25519 DH and derive session keys + SAS via HKDF-SHA256.

        Must be called after both local and remote handshake data are set.
        Ephemeral private key is zeroed after this point.
        """
        if self._eph_private is None:
            raise RuntimeError("Ephemeral private key not available")
        if self._local_hs is None or self._remote_hs is None:
            raise RuntimeError("Handshake data incomplete")

        # X25519 DH: derive shared secret from our ephemeral private + their ephemeral public
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        remote_eph_pub = X25519PublicKey.from_public_bytes(self._remote_hs.ephemeral_pub_key)
        dh_shared = self._eph_private.exchange(remote_eph_pub)

        # Discard ephemeral private key immediately
        self._eph_private = None

        # Transcript for HKDF input keying material is deterministic:
        # sorted by static public key to ensure both sides get identical transcript
        # (Initiator key || Responder key depends on who has lower lexicographic key)
        if self._is_initiator:
            init_static = self._local_hs.static_pub_key
            resp_static = self._remote_hs.static_pub_key
            init_eph = self._local_hs.ephemeral_pub_key
            resp_eph = self._remote_hs.ephemeral_pub_key
            init_nonce = self._local_hs.nonce
            resp_nonce = self._remote_hs.nonce
        else:
            init_static = self._remote_hs.static_pub_key
            resp_static = self._local_hs.static_pub_key
            init_eph = self._remote_hs.ephemeral_pub_key
            resp_eph = self._local_hs.ephemeral_pub_key
            init_nonce = self._remote_hs.nonce
            resp_nonce = self._local_hs.nonce

        # Transcript = all public material, ordered deterministically
        transcript_salt = (
            init_static + resp_static +
            init_eph + resp_eph +
            init_nonce + resp_nonce
        )

        # Derive initiator send key (= responder recv key)
        init_key = HKDF(
            algorithm=crypto_hashes.SHA256(),
            length=SESSION_KEY_LEN,
            salt=transcript_salt,
            info=SESSION_KEY_INFO_INITIATOR,
        ).derive(dh_shared)

        # Derive responder send key (= initiator recv key)
        resp_key = HKDF(
            algorithm=crypto_hashes.SHA256(),
            length=SESSION_KEY_LEN,
            salt=transcript_salt,
            info=SESSION_KEY_INFO_RESPONDER,
        ).derive(dh_shared)

        # SAS: derived from DH shared secret + full transcript salt
        sas_raw = HKDF(
            algorithm=crypto_hashes.SHA256(),
            length=SAS_BYTES,
            salt=transcript_salt,
            info=SAS_HKDF_INFO,
        ).derive(dh_shared)

        if self._is_initiator:
            self._derived = DerivedKeys(
                send_key=init_key,
                recv_key=resp_key,
                sas_bytes=sas_raw,
            )
        else:
            self._derived = DerivedKeys(
                send_key=resp_key,
                recv_key=init_key,
                sas_bytes=sas_raw,
            )

        self._send_cipher = ChaCha20Poly1305(self._derived.send_key)
        self._recv_cipher = ChaCha20Poly1305(self._derived.recv_key)
        return self._derived

    # ------------------------------------------------------------------
    # Auth transcript for Ed25519 signing
    # ------------------------------------------------------------------

    def build_auth_transcript(self) -> bytes:
        """
        Build the canonical byte string that both sides sign with Ed25519.

        This binds the authentication to the full handshake context,
        preventing any attacker from reusing a signature from another session.
        """
        if self._local_hs is None or self._remote_hs is None:
            raise RuntimeError("Handshake data incomplete")

        # Canonical: initiator fields first, then responder fields
        # (same determinism as key derivation)
        if self._is_initiator:
            return (
                b"ferry-auth-v1|" +
                self._local_hs.static_pub_key +
                self._local_hs.ephemeral_pub_key +
                self._local_hs.nonce +
                self._remote_hs.static_pub_key +
                self._remote_hs.ephemeral_pub_key +
                self._remote_hs.nonce
            )
        else:
            return (
                b"ferry-auth-v1|" +
                self._remote_hs.static_pub_key +
                self._remote_hs.ephemeral_pub_key +
                self._remote_hs.nonce +
                self._local_hs.static_pub_key +
                self._local_hs.ephemeral_pub_key +
                self._local_hs.nonce
            )

    # ------------------------------------------------------------------
    # AEAD framing (post-handshake)
    # ------------------------------------------------------------------

    def encrypt_frame(self, plaintext: bytes) -> bytes:
        """
        Encrypt plaintext into an AEAD frame.

        Frame format:
          [4-byte big-endian ciphertext_len] [12-byte nonce] [ciphertext+tag]

        The nonce embeds the send counter to prevent reuse.
        """
        if self._send_cipher is None:
            raise RuntimeError("Session keys not derived; cannot encrypt")
        if len(plaintext) > MAX_AEAD_PLAINTEXT:
            raise ValueError(f"Plaintext {len(plaintext)} exceeds max {MAX_AEAD_PLAINTEXT}")

        nonce = self._make_nonce(self._send_counter)
        self._send_counter += 1

        ciphertext = self._send_cipher.encrypt(nonce, plaintext, None)
        frame = struct.pack("!I", len(ciphertext)) + nonce + ciphertext
        return frame

    def decrypt_frame(self, frame: bytes) -> bytes:
        """
        Decrypt an AEAD frame received from the peer.

        Raises ValueError on authentication failure or malformed frame.
        Uses the expected receive counter as part of the nonce.
        """
        if self._recv_cipher is None:
            raise RuntimeError("Session keys not derived; cannot decrypt")
        if len(frame) < AEAD_HEADER_SIZE + AEAD_NONCE_SIZE:
            raise ValueError("Frame too short")

        (ct_len,) = struct.unpack("!I", frame[:4])
        expected_total = AEAD_HEADER_SIZE + AEAD_NONCE_SIZE + ct_len
        if len(frame) < expected_total:
            raise ValueError("Frame truncated")

        frame_nonce = frame[4:4 + AEAD_NONCE_SIZE]
        ciphertext = frame[4 + AEAD_NONCE_SIZE: expected_total]

        # Verify the nonce counter matches what we expect (replay protection)
        expected_nonce = self._make_nonce(self._recv_counter)
        if frame_nonce != expected_nonce:
            raise ValueError(
                f"Nonce mismatch: expected counter {self._recv_counter}"
            )
        self._recv_counter += 1

        try:
            return self._recv_cipher.decrypt(frame_nonce, ciphertext, None)
        except Exception as exc:
            raise ValueError(f"AEAD authentication failed: {exc}") from exc

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def transition(self, new_state: SessionState) -> None:
        """Advance session state. Invalid transitions raise RuntimeError."""
        valid: dict[SessionState, set[SessionState]] = {
            SessionState.DISCONNECTED: {SessionState.CONNECTING},
            SessionState.CONNECTING: {SessionState.HANDSHAKING, SessionState.FAILED},
            SessionState.HANDSHAKING: {SessionState.AUTHENTICATING, SessionState.FAILED},
            SessionState.AUTHENTICATING: {SessionState.ESTABLISHED, SessionState.PAIRING, SessionState.FAILED},
            SessionState.PAIRING: {SessionState.WAITING_FOR_LOCAL_DECISION, SessionState.WAITING_FOR_REMOTE_DECISION, SessionState.FAILED},
            SessionState.WAITING_FOR_LOCAL_DECISION: {SessionState.WAITING_FOR_REMOTE_DECISION, SessionState.PAIR_ACCEPTED, SessionState.FAILED},
            SessionState.WAITING_FOR_REMOTE_DECISION: {SessionState.WAITING_FOR_LOCAL_DECISION, SessionState.PAIR_ACCEPTED, SessionState.FAILED},
            SessionState.PAIR_ACCEPTED: {SessionState.ESTABLISHED, SessionState.FAILED},
            SessionState.ESTABLISHED: {SessionState.CLOSING, SessionState.FAILED},
            SessionState.CLOSING: {SessionState.DISCONNECTED},
            SessionState.FAILED: {SessionState.DISCONNECTED},
        }
        allowed = valid.get(self._state, set())
        if new_state not in allowed:
            raise RuntimeError(
                f"Invalid session state transition: {self._state} → {new_state}"
            )
        self._state = new_state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_nonce(counter: int) -> bytes:
        """Build a 12-byte nonce with counter encoded in last 8 bytes."""
        # First 4 bytes zero, last 8 bytes = big-endian uint64 counter
        return b"\x00" * 4 + struct.pack("!Q", counter)
