"""
Ferry Linux Core Service Daemon.

Manages the Ferry service lifecycle:
- mDNS/DNS-SD advertisement and discovery (Phase 2A)
- TCP control-plane listener and connection handler (Phase 2B)
- Secure handshake and session management (Phase 2B)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import signal
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional, Set

from .config import ConfigManager, FerryConfig
from .db import DatabaseManager, TrustedDevice
from .discovery import DiscoveredDevice, DiscoveryManager
from .identity import IdentityManager
from .session import FerrySession, HandshakeData, SessionState
from ..protocol.models import (
    FerryEnvelope,
    MessageType,
    decode_frame,
    encode_frame,
)

logger = logging.getLogger("ferry.service")

# Handshake / session timeouts
HANDSHAKE_TIMEOUT_SECS = 30
AUTH_TIMEOUT_SECS = 30

# Simultaneous connect tiebreaker: side with lexicographically lower device_id yields
# and becomes responder; the other becomes initiator.


class PeerSession:
    """Represents one active or establishing session with a remote Ferry peer."""

    def __init__(
        self,
        session: FerrySession,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        remote_addr: str,
    ) -> None:
        self.session = session
        self.reader = reader
        self.writer = writer
        self.remote_addr = remote_addr
        self.remote_device_id: Optional[str] = None
        self.remote_device_name: Optional[str] = None
        self.remote_static_pub_b64: Optional[str] = None

    @property
    def state(self) -> SessionState:
        return self.session.state


class FerryService:
    """Core daemon managing local networking, discovery, pairing, and sessions."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.config_manager = ConfigManager(base_dir=base_dir)
        self.config: FerryConfig = self.config_manager.load_config()
        self.db = DatabaseManager(self.config_manager.db_file)
        self.discovery = DiscoveryManager(self.config)
        self.identity = IdentityManager(self.config_manager.data_dir / "identity.key")
        self._is_running = False
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_tasks: Set[asyncio.Task] = set()
        self._active_sessions: Dict[str, PeerSession] = {}  # remote_addr -> PeerSession
        self._session_listeners: list[Callable[[str, SessionState], None]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def discovered_devices(self) -> list[DiscoveredDevice]:
        """Return list of currently discovered peers on local network."""
        return self.discovery.get_devices()

    @property
    def local_identity_public_key_b64(self) -> str:
        """Return the local Ed25519 public key (base64url, no padding)."""
        return self.identity.public_key_b64

    def add_session_listener(self, cb: Callable[[str, SessionState], None]) -> None:
        """Register callback for session state changes: cb(remote_addr, new_state)."""
        self._session_listeners.append(cb)

    def _notify_session_change(self, remote_addr: str, state: SessionState) -> None:
        for cb in self._session_listeners:
            try:
                cb(remote_addr, state)
            except Exception as exc:
                logger.error("Session listener error: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the Ferry service: discovery + TCP listener."""
        if self._is_running:
            return

        logger.info("Starting Ferry Service on port %d...", self.config.listen_port)
        self._is_running = True

        # Ensure download directory exists
        Path(self.config.download_dir).mkdir(parents=True, exist_ok=True)

        # Start mDNS discovery
        await self.discovery.start()

        # Start TCP server
        self._server = await asyncio.start_server(
            self._handle_incoming_connection,
            host="0.0.0.0",
            port=self.config.listen_port,
        )
        logger.info(
            "Ferry Service is active. Device ID: %s, Name: %s, Identity: %s...",
            self.config.device_id or "unassigned",
            self.config.device_name,
            self.identity.public_key_b64[:12],
        )

    async def stop(self) -> None:
        """Gracefully stop the service."""
        if not self._is_running:
            return

        logger.info("Stopping Ferry Service...")
        self._is_running = False

        await self.discovery.stop()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        for task in list(self._active_tasks):
            task.cancel()

        logger.info("Ferry Service stopped successfully.")

    async def run_forever(self) -> None:
        """Run the service until SIGINT/SIGTERM is received."""
        await self.start()
        stop_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        await stop_event.wait()
        await self.stop()

    # ------------------------------------------------------------------
    # Outbound connection (initiator)
    # ------------------------------------------------------------------

    async def connect_to_peer(self, device: DiscoveredDevice) -> Optional["PeerSession"]:
        """
        Initiate a secure control session to a discovered peer.
        Returns PeerSession if ESTABLISHED, None on failure.
        """
        if not device.addresses:
            logger.warning("No address available for %s", device.device_name)
            return None

        host = device.addresses[0]
        port = device.port

        try:
            logger.info("Connecting to peer %s at %s:%d...", device.device_name, host, port)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=10.0,
            )
        except Exception as exc:
            logger.error("Failed to connect to %s: %s", device.device_name, exc)
            return None

        remote_addr = f"{host}:{port}"
        session = FerrySession(is_initiator=True)
        peer_session = PeerSession(session, reader, writer, remote_addr)

        # Event signalled when handshake completes (success or failure)
        handshake_done = asyncio.Event()

        task = asyncio.create_task(
            self._run_handshake(peer_session, is_initiator=True, handshake_done=handshake_done)
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

        # Wait only for the handshake phase to complete
        try:
            await asyncio.wait_for(
                handshake_done.wait(),
                timeout=HANDSHAKE_TIMEOUT_SECS + AUTH_TIMEOUT_SECS + 5,
            )
        except asyncio.TimeoutError:
            logger.error("Handshake to %s timed out", device.device_name)
            task.cancel()
            return None

        if peer_session.state == SessionState.ESTABLISHED:
            return peer_session
        return None

    # ------------------------------------------------------------------
    # Incoming connection handler
    # ------------------------------------------------------------------

    async def _handle_incoming_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        addr = writer.get_extra_info("peername")
        remote_addr = f"{addr[0]}:{addr[1]}" if addr else "unknown"
        logger.info("Incoming connection from %s", remote_addr)

        session = FerrySession(is_initiator=False)
        peer_session = PeerSession(session, reader, writer, remote_addr)

        task = asyncio.create_task(
            self._run_handshake(peer_session, is_initiator=False, handshake_done=None)
        )
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

    # ------------------------------------------------------------------
    # Handshake state machine
    # ------------------------------------------------------------------

    async def _run_handshake(
        self,
        ps: PeerSession,
        is_initiator: bool,
        handshake_done: Optional[asyncio.Event] = None,
    ) -> None:
        """Full handshake flow for one peer connection."""
        session = ps.session
        reader = ps.reader
        writer = ps.writer

        try:
            session.transition(SessionState.CONNECTING)
            session.transition(SessionState.HANDSHAKING)

            # --- Step 1: Exchange HANDSHAKE_INIT / HANDSHAKE_RESPONSE ---
            local_hs = session.build_local_handshake(self.identity.public_key_bytes)
            init_payload = {
                "device_id": self.config.device_id,
                "device_name": self.config.device_name,
                "device_type": "desktop",
                "public_key": self.identity.public_key_b64,
                "ephemeral_key": base64.urlsafe_b64encode(local_hs.ephemeral_pub_key).rstrip(b"=").decode(),
                "nonce": base64.urlsafe_b64encode(local_hs.nonce).rstrip(b"=").decode(),
                "is_paired": False,  # updated below
            }

            if is_initiator:
                await self._send_plain(writer, MessageType.HANDSHAKE_INIT, init_payload)
                remote_env = await asyncio.wait_for(
                    self._recv_plain(reader), timeout=HANDSHAKE_TIMEOUT_SECS
                )
                if remote_env is None or remote_env.type != MessageType.HANDSHAKE_RESPONSE:
                    raise ValueError(f"Expected HANDSHAKE_RESPONSE, got {remote_env and remote_env.type}")
                remote_payload = remote_env.payload
            else:
                remote_env = await asyncio.wait_for(
                    self._recv_plain(reader), timeout=HANDSHAKE_TIMEOUT_SECS
                )
                if remote_env is None or remote_env.type != MessageType.HANDSHAKE_INIT:
                    raise ValueError(f"Expected HANDSHAKE_INIT, got {remote_env and remote_env.type}")
                remote_payload = remote_env.payload
                await self._send_plain(writer, MessageType.HANDSHAKE_RESPONSE, init_payload)

            # Parse remote handshake
            remote_static_b64 = remote_payload.get("public_key", "")
            remote_eph_b64 = remote_payload.get("ephemeral_key", "")
            remote_nonce_b64 = remote_payload.get("nonce", "")

            remote_static_bytes = IdentityManager.decode_public_key_b64(remote_static_b64)
            remote_eph_bytes = IdentityManager.decode_public_key_b64(remote_eph_b64)
            remote_nonce_bytes = IdentityManager.decode_public_key_b64(remote_nonce_b64)

            ps.remote_device_id = remote_payload.get("device_id", "")
            ps.remote_device_name = remote_payload.get("device_name", "")
            ps.remote_static_pub_b64 = remote_static_b64

            session.accept_remote_handshake(HandshakeData(
                static_pub_key=remote_static_bytes,
                ephemeral_pub_key=remote_eph_bytes,
                nonce=remote_nonce_bytes,
            ))

            # --- Step 2: Derive session keys ---
            derived = session.derive_keys()
            sas = session.sas_code

            # Check if peer is already trusted
            trusted = self.db.get_device_by_public_key(remote_static_b64)
            is_paired = trusted is not None

            if is_paired:
                session.transition(SessionState.AUTHENTICATING)
                logger.info(
                    "Known peer %s (%s) — proceeding to authentication",
                    ps.remote_device_name, ps.remote_device_id
                )
            else:
                session.transition(SessionState.PAIRING)
                logger.info(
                    "New peer %s — SAS: %s  (auto-accepting for Phase 2B; full UI in Phase 2C)",
                    ps.remote_device_name, sas
                )
                # Phase 2B: auto-accept for testing; Phase 2C will show GNOME dialog
                # Persist trust immediately after SAS (simulated user acceptance)
                now = int(time.time() * 1000)
                self.db.add_or_update_device(TrustedDevice(
                    device_id=ps.remote_device_id or str(uuid.uuid4()),
                    device_name=ps.remote_device_name or "Unknown",
                    public_key=remote_static_b64,
                    identity_public_key_b64=remote_static_b64,
                    paired_at=now,
                    last_seen=now,
                ))
                logger.info("Peer %s trusted and persisted (SAS: %s)", ps.remote_device_name, sas)
                session.transition(SessionState.AUTHENTICATING)

            # --- Step 3: Mutual Ed25519 authentication over transcript ---
            transcript = session.build_auth_transcript()
            local_sig = self.identity.sign(transcript)
            local_sig_b64 = base64.urlsafe_b64encode(local_sig).rstrip(b"=").decode()

            auth_challenge_payload = {
                "signature": local_sig_b64,
                "transcript_hash": base64.urlsafe_b64encode(
                    __import__("hashlib").sha256(transcript).digest()
                ).decode(),
            }

            if is_initiator:
                await self._send_plain(writer, MessageType.AUTH_CHALLENGE, auth_challenge_payload)
                resp_env = await asyncio.wait_for(
                    self._recv_plain(reader), timeout=AUTH_TIMEOUT_SECS
                )
                if resp_env is None or resp_env.type != MessageType.AUTH_RESPONSE:
                    raise ValueError(f"Expected AUTH_RESPONSE, got {resp_env and resp_env.type}")
                remote_sig_b64 = resp_env.payload.get("signature", "")
            else:
                chal_env = await asyncio.wait_for(
                    self._recv_plain(reader), timeout=AUTH_TIMEOUT_SECS
                )
                if chal_env is None or chal_env.type != MessageType.AUTH_CHALLENGE:
                    raise ValueError(f"Expected AUTH_CHALLENGE, got {chal_env and chal_env.type}")
                remote_sig_b64 = chal_env.payload.get("signature", "")
                await self._send_plain(writer, MessageType.AUTH_RESPONSE, auth_challenge_payload)

            # Verify remote signature
            remote_sig = IdentityManager.decode_public_key_b64(remote_sig_b64)
            if not IdentityManager.verify(remote_static_bytes, transcript, remote_sig):
                raise ValueError("AUTH signature verification failed — rejecting peer")

            logger.info("Auth verified for %s", ps.remote_device_name)

            # Update last_seen
            now = int(time.time() * 1000)
            existing = self.db.get_device_by_public_key(remote_static_b64)
            if existing:
                self.db.add_or_update_device(TrustedDevice(
                    device_id=existing.device_id,
                    device_name=existing.device_name,
                    public_key=existing.public_key,
                    identity_public_key_b64=existing.identity_public_key_b64,
                    paired_at=existing.paired_at,
                    last_seen=now,
                ))

            session.transition(SessionState.ESTABLISHED)
            self._active_sessions[ps.remote_addr] = ps
            self._notify_session_change(ps.remote_addr, SessionState.ESTABLISHED)
            logger.info("Session ESTABLISHED with %s (%s)", ps.remote_device_name, ps.remote_addr)

            # Signal handshake completion to connect_to_peer waiter (if any)
            if handshake_done is not None:
                handshake_done.set()

            # --- Step 4: Encrypted session loop ---
            await self._run_session_loop(ps)

        except asyncio.CancelledError:
            logger.info("Session with %s cancelled", ps.remote_addr)
        except Exception as exc:
            logger.error("Session with %s failed: %s", ps.remote_addr, exc)
            if session.state not in (SessionState.ESTABLISHED, SessionState.CLOSING, SessionState.FAILED, SessionState.DISCONNECTED):
                try:
                    session.transition(SessionState.FAILED)
                except Exception:
                    pass
            self._notify_session_change(ps.remote_addr, SessionState.FAILED)
        finally:
            # Always signal the handshake waiter so connect_to_peer doesn't hang
            if handshake_done is not None and not handshake_done.is_set():
                handshake_done.set()
            self._active_sessions.pop(ps.remote_addr, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if session.state not in (SessionState.DISCONNECTED, SessionState.FAILED):
                try:
                    session.transition(SessionState.CLOSING)
                    session.transition(SessionState.DISCONNECTED)
                except Exception:
                    pass
            self._notify_session_change(ps.remote_addr, session.state)

    # ------------------------------------------------------------------
    # Encrypted session loop (post-ESTABLISHED)
    # ------------------------------------------------------------------

    async def _run_session_loop(self, ps: PeerSession) -> None:
        """Read and process encrypted frames from an ESTABLISHED session."""
        try:
            while True:
                frame_data = await asyncio.wait_for(
                    self._read_aead_frame_bytes(ps.reader),
                    timeout=300.0,  # 5-minute idle timeout
                )
                if frame_data is None:
                    logger.info("Peer %s closed connection", ps.remote_addr)
                    break

                plaintext = ps.session.decrypt_frame(frame_data)
                envelope = FerryEnvelope.from_json(plaintext.decode("utf-8"))

                if envelope.type == MessageType.DISCONNECT:
                    logger.info("Peer %s sent DISCONNECT", ps.remote_addr)
                    break

                logger.debug("Received encrypted message type=%s from %s", envelope.type, ps.remote_addr)
                # Future: route to transfer handler etc.

        except asyncio.TimeoutError:
            logger.info("Session idle timeout with %s", ps.remote_addr)
        except Exception as exc:
            logger.error("Session loop error with %s: %s", ps.remote_addr, exc)
        finally:
            ps.session.transition(SessionState.CLOSING)
            ps.session.transition(SessionState.DISCONNECTED)

    # ------------------------------------------------------------------
    # Frame I/O helpers
    # ------------------------------------------------------------------

    async def _send_plain(
        self,
        writer: asyncio.StreamWriter,
        msg_type: MessageType,
        payload: dict,
    ) -> None:
        """Send an unencrypted Ferry frame (handshake phase only)."""
        envelope = FerryEnvelope(type=msg_type.value, payload=payload)
        frame = encode_frame(envelope)
        writer.write(frame)
        await writer.drain()

    async def _recv_plain(self, reader: asyncio.StreamReader) -> Optional[FerryEnvelope]:
        """Read one unencrypted Ferry frame (handshake phase only)."""
        header = await reader.readexactly(6)
        if not header:
            return None
        from ..protocol.models import MAGIC_BYTES, MAX_CONTROL_FRAME_SIZE
        import struct
        if header[:2] != MAGIC_BYTES:
            raise ValueError(f"Invalid magic bytes: {header[:2]!r}")
        (length,) = struct.unpack("!I", header[2:6])
        if length > MAX_CONTROL_FRAME_SIZE:
            raise ValueError("Frame too large")
        payload_bytes = await reader.readexactly(length)
        return FerryEnvelope.from_json(payload_bytes.decode("utf-8"))

    async def _read_aead_frame_bytes(self, reader: asyncio.StreamReader) -> Optional[bytes]:
        """
        Read one raw AEAD frame from the stream (without decrypting).
        Returns the raw bytes including header+nonce+ciphertext for decrypt_frame().
        """
        from ..core.session import AEAD_HEADER_SIZE, AEAD_NONCE_SIZE
        import struct
        header = await reader.readexactly(AEAD_HEADER_SIZE)
        if not header:
            return None
        (ct_len,) = struct.unpack("!I", header)
        rest = await reader.readexactly(AEAD_NONCE_SIZE + ct_len)
        return header + rest

    async def send_encrypted(self, ps: PeerSession, msg_type: MessageType, payload: dict) -> None:
        """Send an encrypted Ferry envelope to an ESTABLISHED session peer."""
        if ps.state != SessionState.ESTABLISHED:
            raise RuntimeError(f"Cannot send to session in state {ps.state}")
        envelope = FerryEnvelope(type=msg_type.value, payload=payload)
        plaintext = envelope.to_json().encode("utf-8")
        frame = ps.session.encrypt_frame(plaintext)
        ps.writer.write(frame)
        await ps.writer.drain()
