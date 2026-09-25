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
from .transfer import (
    CHUNK_MAGIC,
    TRANSFER_ACCEPT_TIMEOUT_SECS,
    TRANSFER_CHUNK_TIMEOUT_SECS,
    IncomingTransfer,
    OutgoingTransfer,
    TransferError,
    TransferMetadata,
    TransferState,
    is_chunk_frame,
    decode_chunk_frame,
    BatchState,
    BatchIncomingTransfer,
    BatchOutgoingTransfer,
)
from .clipboard import ClipboardSyncManager
from .notifications import NotificationManager
from .notification_bridge import NotificationBridge
from .ipc import FerryIPCServer
from ..protocol.models import (
    FerryEnvelope,
    MessageType,
    TransferRequestPayload,
    TransferAcceptPayload,
    TransferRejectPayload,
    TransferCompletePayload,
    TransferResultPayload,
    TransferErrorPayload,
    BatchRequestPayload,
    BatchAcceptPayload,
    BatchRejectPayload,
    BatchCancelPayload,
    BatchCompletePayload,
    NotificationPostPayload,
    NotificationRemovePayload,
    MAX_NOTIFICATION_FRAME_BYTES,
    decode_frame,
    encode_frame,
)

logger = logging.getLogger("ferry.service")

# Handshake / session timeouts
HANDSHAKE_TIMEOUT_SECS = 30
AUTH_TIMEOUT_SECS = 30

# Phase 3D: guard outgoing-transfer state changes from concurrent access
_TRANSFER_LOCK = None  # set lazily per event loop


def _compute_prefix_sha256(file_path: Path, length_bytes: int) -> Optional[str]:
    """Compute SHA-256 of the first length_bytes bytes of file_path.
    Returns None if the file is shorter than length_bytes, else the hex digest."""
    import hashlib as _hashlib
    try:
        digest = _hashlib.sha256()
        remaining = length_bytes
        with open(file_path, "rb") as fh:
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    # file shorter than requested prefix
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest()
    except OSError:
        return None


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
        self._transfer_request_listeners = []
        self._transfer_progress_listeners = []
        self._transfer_complete_listeners = []
        # Active incoming transfers keyed by transfer_id
        self._incoming_transfers: Dict[str, IncomingTransfer] = {}
        self._incoming_peers: Dict[str, PeerSession] = {}
        # Active outgoing transfers keyed by transfer_id -> (PeerSession, OutgoingTransfer)
        self._active_outgoing_transfers: Dict[str, tuple[PeerSession, OutgoingTransfer]] = {}
        self._active_batches: Dict[str, BatchIncomingTransfer] = {}
        # Phase 3D: acceptance timeout tasks keyed by transfer_id
        self._accept_timeout_tasks: Dict[str, asyncio.Task] = {}
        # Phase 3D: track last chunk time for stall detection, keyed by transfer_id
        self._last_chunk_time: Dict[str, float] = {}
        # Phase 3D: set of transfer_ids already recorded in history (prevents duplicates)
        self._recorded_transfer_ids: set = set()
        # MVP: Clipboard sync manager
        # Disabled per final product scope
        # MVP: Notification manager (app reference set by UI layer)
        self.notifications: NotificationManager = NotificationManager()
        # Phase 5: D-Bus notification bridge for mirrored Android notifications
        self.notification_bridge: NotificationBridge = NotificationBridge()
        self.notification_bridge.start()
        # P1: IPC server for local UI clients
        self.ipc_server: FerryIPCServer = FerryIPCServer(self)

        # Wire clipboard send callback
        async def _send_clipboard_to_peer(text: str, peer_id: str) -> None:
            ps = self._active_sessions.get(peer_id)
            if ps and ps.state == SessionState.ESTABLISHED:
                try:
                    await self.send_encrypted(
                        ps,
                        MessageType.CLIPBOARD_SYNC,
                        {"text": text, "ts": int(time.time() * 1000)}
                    )
                except Exception as exc:
                    logger.debug("Failed to send CLIPBOARD_SYNC to %s: %s", peer_id, exc)


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

    def add_transfer_request_listener(self, cb) -> None:
        """Register callback for incoming transfer requests: cb(remote_addr, transfer_id, file_name, file_size)."""
        self._transfer_request_listeners.append(cb)

    def add_transfer_progress_listener(self, cb) -> None:
        """Register callback for transfer progress: cb(transfer_id, bytes_done, total_bytes)."""
        self._transfer_progress_listeners.append(cb)

    def add_transfer_complete_listener(self, cb) -> None:
        """Register callback for transfer completion: cb(transfer_id, success, file_name, direction)."""
        self._transfer_complete_listeners.append(cb)

    @property
    def established_sessions(self) -> dict:
        """Return {remote_addr: PeerSession} for all ESTABLISHED sessions."""
        return {
            addr: ps
            for addr, ps in self._active_sessions.items()
            if ps.state == SessionState.ESTABLISHED
        }

    def _notify_session_change(self, remote_addr: str, state: SessionState) -> None:
        for cb in self._session_listeners:
            try:
                cb(remote_addr, state)
            except Exception as exc:
                logger.error("Session listener error: %s", exc)

    def _notify_transfer_progress(self, transfer_id: str, bytes_done: int, total_bytes: int) -> None:
        for cb in self._transfer_progress_listeners:
            try:
                cb(transfer_id, bytes_done, total_bytes)
            except Exception as exc:
                logger.error("Transfer progress listener error: %s", exc)

    def _notify_transfer_complete(self, transfer_id: str, success: bool, file_name: str, direction: str) -> None:
        for cb in self._transfer_complete_listeners:
            try:
                cb(transfer_id, success, file_name, direction)
            except Exception as exc:
                logger.error("Transfer complete listener error: %s", exc)

    # ------------------------------------------------------------------
    # Phase 3D helpers
    # ------------------------------------------------------------------

    def _record_transfer_once(
        self,
        transfer_id: str,
        device_id: str,
        file_name: str,
        file_size: int,
        direction: str,
        status: str,
        started_at: int,
        sha256: str,
    ) -> None:
        """Write a transfer history record, but only once per transfer_id."""
        if transfer_id in self._recorded_transfer_ids:
            logger.debug(
                "Transfer %s already recorded; skipping duplicate history entry",
                transfer_id[:8],
            )
            return
        from .db import TransferRecord
        try:
            self.db.add_transfer(TransferRecord(
                transfer_id=transfer_id,
                device_id=device_id,
                file_name=file_name,
                file_size=file_size,
                direction=direction,
                status=status,
                started_at=started_at,
                completed_at=int(time.time() * 1000),
                sha256=sha256,
            ))
            self._recorded_transfer_ids.add(transfer_id)
        except Exception as exc:
            logger.warning("Failed to record transfer %s in history: %s", transfer_id[:8], exc)

    def _cancel_incoming_transfer_for_peer(self, ps: "PeerSession") -> None:
        """Cancel all active incoming transfers associated with the given peer session."""
        to_cancel = [
            (tid, xfer)
            for tid, xfer in list(self._incoming_transfers.items())
            if self._incoming_peers.get(tid) is ps
        ]
        for transfer_id, incoming in to_cancel:
            self._incoming_transfers.pop(transfer_id, None)
            self._incoming_peers.pop(transfer_id, None)
            # Cancel acceptance timeout if pending
            timeout_task = self._accept_timeout_tasks.pop(transfer_id, None)
            if timeout_task:
                timeout_task.cancel()
            self._last_chunk_time.pop(transfer_id, None)
            incoming.cancel()
            device_id = ps.remote_device_id or ps.remote_addr
            
            final_status = "INTERRUPTED" if incoming.state == TransferState.INTERRUPTED else "FAILED"
            logger.info(
                "Incoming transfer %s %s due to peer disconnect", transfer_id[:8], final_status
            )
            self._record_transfer_once(
                transfer_id=transfer_id,
                device_id=device_id,
                file_name=incoming.meta.file_name,
                file_size=incoming.meta.file_size,
                direction="INCOMING",
                status=final_status,
                started_at=incoming.meta.created_at,
                sha256="",
            )
            self._notify_transfer_complete(transfer_id, False, incoming.meta.file_name, "INCOMING")

    def _cancel_outgoing_transfer_for_peer(self, ps: "PeerSession") -> None:
        """Fail all active outgoing transfers for the given peer session."""
        to_cancel = [
            (tid, xfer)
            for tid, (peer, xfer) in list(self._active_outgoing_transfers.items())
            if peer is ps
        ]
        for transfer_id, xfer in to_cancel:
            xfer.cancel()
            # Unblock any waiting events
            self._signal_transfer_event(ps, "_transfer_events", transfer_id, "CANCEL")
            self._signal_transfer_event(ps, "_result_events", transfer_id, False)

    async def accept_pairing(self, remote_addr: str) -> None:
        """User clicked Accept in the pairing dialog."""
        ps = self._active_sessions.get(remote_addr)
        if not ps:
            return
        logger.info("Local user accepted pairing with %s", ps.remote_device_name)
        
        if ps.session.state == SessionState.PAIRING:
            ps.session.transition(SessionState.WAITING_FOR_REMOTE_DECISION)
        elif ps.session.state == SessionState.WAITING_FOR_LOCAL_DECISION:
            ps.session.transition(SessionState.PAIR_ACCEPTED)
            self._persist_trust(ps)
            ps.session.transition(SessionState.ESTABLISHED)
        
        self._notify_session_change(ps.remote_addr, ps.session.state)
        await self.send_encrypted(ps, MessageType.PAIR_DECISION, {"decision": "ACCEPT"})

    async def reject_pairing(self, remote_addr: str) -> None:
        """User clicked Reject in the pairing dialog."""
        ps = self._active_sessions.get(remote_addr)
        if not ps:
            return
        logger.info("Local user rejected pairing with %s", ps.remote_device_name)
        try:
            await self.send_encrypted(ps, MessageType.PAIR_DECISION, {"decision": "REJECT"})
        except Exception:
            pass
        self._disconnect_session(ps)

    async def discard_interrupted_transfer(self, transfer_id: str) -> None:
        """Discard a previously-interrupted transfer: delete .part file and remove DB record."""
        # Remove the .part file
        staging_dir = Path(self.config.download_dir) / "staging"
        part_file = staging_dir / f"{transfer_id}.part"
        if part_file.exists():
            try:
                part_file.unlink()
                logger.info("Discarded .part file for interrupted transfer %s", transfer_id[:8])
            except OSError as exc:
                logger.warning("Could not delete .part for %s: %s", transfer_id[:8], exc)
        # Remove from in-memory interrupted transfers
        self._incoming_transfers.pop(transfer_id, None)
        self._incoming_peers.pop(transfer_id, None)
        # Remove the DB record
        try:
            self.db.delete_interrupted_transfer(transfer_id)
        except Exception as exc:
            logger.warning("Could not delete interrupted DB record for %s: %s", transfer_id[:8], exc)

    async def send_batch(self, remote_addr: str, source_paths: list[Path], batch_name: str) -> bool:
        """
        Phase 4C: Initiate a batch file/directory transfer.
        Enumerates all files recursively, sends BATCH_REQUEST, and streams items.
        """
        ps = self._active_sessions.get(remote_addr)
        if ps is None or ps.state != SessionState.ESTABLISHED:
            logger.error(f"Cannot send batch to {remote_addr}: no established session")
            return False

        # Recursively find all files
        all_files: list[tuple[Path, str]] = [] # (absolute_path, relative_path)
        total_bytes = 0

        import os
        for path in source_paths:
            path = path.resolve()
            if not path.exists():
                continue
            if path.is_file():
                all_files.append((path, path.name))
                total_bytes += path.stat().st_size
            elif path.is_dir():
                base_dir = path.parent
                for root, _, files in os.walk(path):
                    for f in files:
                        filepath = Path(root) / f
                        if filepath.is_file() and not filepath.is_symlink():
                            try:
                                rel_path = filepath.relative_to(base_dir).as_posix()
                                all_files.append((filepath, rel_path))
                                total_bytes += filepath.stat().st_size
                            except Exception as e:
                                logger.warning(f"Skipping {filepath}: {e}")

        total_items = len(all_files)
        if total_items == 0:
            logger.error("Batch is empty, nothing to send.")
            return False

        batch_id = str(uuid.uuid4())
        req = BatchRequestPayload(
            batch_id=batch_id,
            batch_name=batch_name,
            total_items=total_items,
            total_bytes=total_bytes,
            sender_identity=self.identity.public_key_b64,
            created_at=int(time.time() * 1000)
        )

        batch = BatchOutgoingTransfer(batch_id, batch_name, total_items, total_bytes, [p for p, _ in all_files])
        self._active_batches[batch_id] = batch

        logger.info(f"Sending BATCH_REQUEST {batch_id[:8]} to {remote_addr}")
        await self.send_encrypted(ps, MessageType.BATCH_REQUEST, req.to_dict())

        # Wait for accept
        response = await asyncio.wait_for(
            self._wait_for_transfer_response(ps, batch_id),
            timeout=60.0,
        )
        if response != "ACCEPT":
            logger.warning(f"Batch {batch_id[:8]} rejected by peer")
            self._active_batches.pop(batch_id, None)
            return False

        # Loop through all files and send them
        success_count = 0
        for abs_path, rel_path in all_files:
            if batch_id not in self._active_batches:
                logger.info(f"Batch {batch_id[:8]} cancelled locally during transmission")
                break
            logger.info(f"Batch {batch_id[:8]} sending item {rel_path}")
            # Ensure send_file can access the batch object
            success = await self.send_file(remote_addr, abs_path, batch_id=batch_id, relative_path=rel_path)
            if success:
                success_count += 1
                batch.items_completed += 1
            else:
                logger.warning(f"Batch item failed: {rel_path}")
                batch.items_failed += 1
                
        # Send BATCH_COMPLETE or BATCH_CANCEL depending on if it was cancelled
        if batch_id in self._active_batches:
            await self.send_encrypted(ps, MessageType.BATCH_COMPLETE, {"batch_id": batch_id})
            self._active_batches.pop(batch_id, None)

        status = "COMPLETED" if success_count == total_items else "FAILED"
        self._record_transfer_once(
            transfer_id=batch_id, device_id=ps.remote_device_id or ps.remote_addr,
            file_name=batch_name, file_size=total_bytes,
            direction="OUTGOING", status=status, started_at=req.created_at, sha256="",
        )
        self._notify_transfer_complete(batch_id, success_count == total_items, batch_name, "OUTGOING")
        return success_count == total_items


    async def send_file(
        self,
        remote_addr: str,
        source_path: "Path",
        transfer_id: Optional[str] = None,
        batch_id: str = "",
        relative_path: str = "",
    ) -> bool:
        """
        Initiate an outgoing file transfer to an already-ESTABLISHED peer.

        Returns True if the receiver accepted and the transfer completed with
        verified integrity.  Returns False on reject, cancellation, or error.
        """
        from .db import TransferRecord
        ps = self._active_sessions.get(remote_addr)
        if ps is None:
            raise RuntimeError(f"No active session for {remote_addr}")
        if ps.state != SessionState.ESTABLISHED:
            raise RuntimeError(f"Session {remote_addr} is not ESTABLISHED (state={ps.state})")

        xfer = OutgoingTransfer(
            source_path=source_path,
            receiver_identity=ps.remote_static_pub_b64 or "",
            transfer_id=transfer_id,
            batch_id=batch_id,
            relative_path=relative_path,
        )
        # Phase 3D: guard metadata build (source file may have disappeared)
        try:
            meta = xfer.build_metadata(sender_identity=self.identity.public_key_b64)
        except (OSError, ValueError) as exc:
            logger.error("Cannot build transfer metadata for %s: %s", source_path, exc)
            return False

        started_at = int(time.time() * 1000)
        self._active_outgoing_transfers[meta.transfer_id] = (ps, xfer)

        try:
            # Send TRANSFER_REQUEST
            await self.send_encrypted(ps, MessageType.TRANSFER_REQUEST, meta.to_dict())
            logger.info(
                "TRANSFER_REQUEST sent to %s: %s (%d bytes)",
                ps.remote_device_name, meta.file_name, meta.file_size,
            )

            # Wait for TRANSFER_ACCEPT or TRANSFER_REJECT (max 60s)
            response = "REJECT"
            try:
                response = await asyncio.wait_for(
                    self._wait_for_transfer_response(ps, meta.transfer_id),
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for transfer response for %s", meta.transfer_id[:8])

            if response != "ACCEPT":
                status = "CANCELLED" if response == "CANCEL" or xfer._cancelled else "REJECTED"
                logger.info("Transfer %s not accepted (response=%s, status=%s)", meta.transfer_id[:8], response, status)
                if not batch_id:
                    self._record_transfer_once(
                        transfer_id=meta.transfer_id, device_id=ps.remote_device_id or ps.remote_addr,
                        file_name=meta.file_name, file_size=meta.file_size,
                        direction="OUTGOING", status=status, started_at=started_at, sha256=meta.sha256,
                    )
                    self._notify_transfer_complete(meta.transfer_id, False, meta.file_name, "OUTGOING")
                return False

            logger.info("Transfer %s accepted — streaming chunks", meta.transfer_id[:8])

            # Stream chunks, emitting progress
            bytes_sent = 0
            batch = self._active_batches.get(batch_id) if batch_id else None
            try:
                async for chunk_frame_bytes in xfer.stream_chunks():
                    if xfer._cancelled or xfer.state == TransferState.CANCELLING:
                        break
                    encrypted = ps.session.encrypt_frame(chunk_frame_bytes)
                    ps.writer.write(encrypted)
                    await ps.writer.drain()
                    # Approximate bytes sent (chunk frame minus 28-byte header)
                    from .transfer import CHUNK_HEADER_SIZE
                    chunk_payload_size = max(0, len(chunk_frame_bytes) - CHUNK_HEADER_SIZE)
                    bytes_sent += chunk_payload_size
                    
                    if batch:
                        batch.bytes_transferred += chunk_payload_size
                        self._notify_transfer_progress(batch_id, batch.bytes_transferred, batch.total_bytes)
                    else:
                        self._notify_transfer_progress(meta.transfer_id, bytes_sent, meta.file_size)

            except TransferError as exc:
                logger.error(
                    "Source read error during outgoing transfer %s: %s",
                    meta.transfer_id[:8], exc,
                )
                try:
                    await self.send_encrypted(
                        ps, MessageType.TRANSFER_CANCEL,
                        {"transfer_id": meta.transfer_id, "reason": "IO_ERROR"}
                    )
                except Exception:
                    pass
                if not batch_id:
                    self._record_transfer_once(
                        transfer_id=meta.transfer_id, device_id=ps.remote_device_id or ps.remote_addr,
                        file_name=meta.file_name, file_size=meta.file_size,
                        direction="OUTGOING", status="FAILED", started_at=started_at, sha256=meta.sha256,
                    )
                    self._notify_transfer_complete(meta.transfer_id, False, meta.file_name, "OUTGOING")
                return False

            if xfer._cancelled or xfer.state == TransferState.CANCELLING:
                logger.info("Outgoing transfer %s cancelled by user during streaming", meta.transfer_id[:8])
                try:
                    await self.send_encrypted(
                        ps, MessageType.TRANSFER_CANCEL,
                        {"transfer_id": meta.transfer_id, "reason": "USER_CANCELLED"}
                    )
                except Exception as exc:
                    logger.debug("Failed sending TRANSFER_CANCEL: %s", exc)
                if not batch_id:
                    self._record_transfer_once(
                        transfer_id=meta.transfer_id, device_id=ps.remote_device_id or ps.remote_addr,
                        file_name=meta.file_name, file_size=meta.file_size,
                        direction="OUTGOING", status="CANCELLED", started_at=started_at, sha256=meta.sha256,
                    )
                    self._notify_transfer_complete(meta.transfer_id, False, meta.file_name, "OUTGOING")
                return False

            # Send TRANSFER_COMPLETE
            await self.send_encrypted(
                ps, MessageType.TRANSFER_COMPLETE, {"transfer_id": meta.transfer_id}
            )
            logger.info("TRANSFER_COMPLETE sent for %s", meta.transfer_id[:8])

            # Wait for TRANSFER_RESULT
            result = False
            try:
                result = await asyncio.wait_for(
                    self._wait_for_transfer_result(ps, meta.transfer_id),
                    timeout=60.0,
                )
                if result:
                    logger.info("Transfer %s completed successfully", meta.transfer_id[:8])
                else:
                    logger.warning("Transfer %s failed integrity check on receiver", meta.transfer_id[:8])
            except asyncio.TimeoutError:
                logger.warning("Timed out waiting for TRANSFER_RESULT for %s", meta.transfer_id[:8])

            if not batch_id:
                self._record_transfer_once(
                    transfer_id=meta.transfer_id, device_id=ps.remote_device_id or ps.remote_addr,
                    file_name=meta.file_name, file_size=meta.file_size,
                    direction="OUTGOING", status="COMPLETED" if result else "FAILED",
                    started_at=started_at, sha256=meta.sha256 if result else "",
                )
                self._notify_transfer_complete(meta.transfer_id, result, meta.file_name, "OUTGOING")

            return result
        finally:
            self._active_outgoing_transfers.pop(meta.transfer_id, None)

    def _record_outgoing_transfer(
        self,
        meta: TransferMetadata,
        ps: "PeerSession",
        status: str,
        started_at: int,
    ) -> None:
        from .db import TransferRecord
        device_id = ps.remote_device_id or ps.remote_addr
        try:
            self.db.add_transfer(TransferRecord(
                transfer_id=meta.transfer_id,
                device_id=device_id,
                file_name=meta.file_name,
                file_size=meta.file_size,
                direction="OUTGOING",
                status=status,
                started_at=started_at,
                completed_at=int(time.time() * 1000),
                sha256=meta.sha256,
            ))
        except Exception as exc:
            logger.warning("Failed to record outgoing transfer in history: %s", exc)

    async def cancel_transfer(self, transfer_id: str, reason: str = "USER_CANCELLED") -> bool:
        """Cancel an active outgoing or incoming transfer."""
        # 0. Batch transfer
        if transfer_id in self._active_batches:
            logger.info("Cancelling batch %s locally", transfer_id[:8])
            batch = self._active_batches.pop(transfer_id, None)
            
            # Find the peer
            # For outgoing, it's whatever peer is in _active_outgoing_transfers that matches the batch?
            # Or we can just find any peer. Let's just broadcast BATCH_CANCEL to all for now or find the peer.
            # Actually, we can just send BATCH_CANCEL to everyone since we don't know the peer easily.
            # Or if it's incoming, we know from _incoming_peers?
            # Actually, `send_batch` checks `self._active_batches`, so popping it is enough to break the loop!
            # For incoming batch, popping it is also enough to ignore further TRANSFER_REQUESTs!
            # We should try to send BATCH_CANCEL though.
            for ps in self._active_sessions.values():
                try:
                    await self.send_encrypted(ps, MessageType.BATCH_CANCEL, {"batch_id": transfer_id})
                except Exception:
                    pass
            
            if batch:
                self._record_transfer_once(
                    transfer_id=transfer_id, device_id="unknown",
                    file_name=batch.batch_name, file_size=batch.total_bytes,
                    direction="OUTGOING", status="CANCELLED", started_at=int(time.time() * 1000), sha256="",
                )
                self._notify_transfer_complete(transfer_id, False, batch.batch_name, "OUTGOING")
            return True

        # 1. Outgoing transfer
        if transfer_id in self._active_outgoing_transfers:
            ps, xfer = self._active_outgoing_transfers[transfer_id]
            logger.info("Cancelling outgoing transfer %s locally", transfer_id[:8])
            xfer.cancel()
            self._signal_transfer_event(ps, "_transfer_events", transfer_id, "CANCEL")
            self._signal_transfer_event(ps, "_result_events", transfer_id, False)
            return True

        # 2. Incoming transfer
        if transfer_id in self._incoming_transfers:
            incoming = self._incoming_transfers.pop(transfer_id, None)
            ps = self._incoming_peers.pop(transfer_id, None)
            # Cancel acceptance timeout
            timeout_task = self._accept_timeout_tasks.pop(transfer_id, None)
            if timeout_task:
                timeout_task.cancel()
            self._last_chunk_time.pop(transfer_id, None)
            if incoming:
                logger.info("Cancelling incoming transfer %s locally", transfer_id[:8])
                incoming.cancel()
                if ps and ps.writer:
                    try:
                        await self.send_encrypted(
                            ps, MessageType.TRANSFER_CANCEL,
                            {"transfer_id": transfer_id, "reason": reason}
                        )
                    except Exception as exc:
                        logger.debug("Failed sending TRANSFER_CANCEL: %s", exc)
                device_id = ps.remote_device_id or ps.remote_addr if ps else "unknown"
                self._record_transfer_once(
                    transfer_id=transfer_id, device_id=device_id,
                    file_name=incoming.meta.file_name, file_size=incoming.meta.file_size,
                    direction="INCOMING", status="CANCELLED",
                    started_at=int(time.time() * 1000), sha256="",
                )
                self._notify_transfer_complete(transfer_id, False, incoming.meta.file_name, "INCOMING")
                return True

        return False

    async def _wait_for_transfer_response(self, ps: "PeerSession", transfer_id: str) -> str:
        """Block until we receive TRANSFER_ACCEPT or TRANSFER_REJECT for this transfer_id."""
        # We use an asyncio.Event per transfer stored in ps
        if not hasattr(ps, "_transfer_events"):
            ps._transfer_events = {}
        event: asyncio.Event = asyncio.Event()
        result_holder: list[str] = []
        ps._transfer_events[transfer_id] = (event, result_holder)
        await event.wait()
        del ps._transfer_events[transfer_id]
        return result_holder[0] if result_holder else "REJECT"

    async def _wait_for_transfer_result(self, ps: "PeerSession", transfer_id: str) -> bool:
        """Block until we receive TRANSFER_RESULT for this transfer_id."""
        if not hasattr(ps, "_result_events"):
            ps._result_events = {}
        event: asyncio.Event = asyncio.Event()
        result_holder: list[bool] = []
        ps._result_events[transfer_id] = (event, result_holder)
        await event.wait()
        del ps._result_events[transfer_id]
        return result_holder[0] if result_holder else False

    def _persist_trust(self, ps: PeerSession) -> None:
        """Persist trust for a paired peer."""
        now = int(time.time() * 1000)
        self.db.add_or_update_device(TrustedDevice(
            device_id=ps.remote_device_id or str(uuid.uuid4()),
            device_name=ps.remote_device_name or "Unknown",
            public_key=ps.remote_static_pub_b64,
            identity_public_key_b64=ps.remote_static_pub_b64,
            paired_at=now,
            last_seen=now,
        ))
        logger.info("Trust persisted for peer %s", ps.remote_device_name)

    def _disconnect_session(self, ps: PeerSession) -> None:
        if ps.session.state not in (SessionState.DISCONNECTED, SessionState.FAILED):
            try:
                ps.session.transition(SessionState.FAILED)
            except Exception:
                pass
        self._notify_session_change(ps.remote_addr, ps.session.state)
        try:
            ps.writer.close()
        except Exception:
            pass

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
        download_dir = Path(self.config.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        # Phase 3D/3E: Clean up stale .part files from previous crashed sessions,
        # but protect those that belong to a valid INTERRUPTED transfer in the DB.
        import time
        staging_dir = download_dir / "staging"
        if staging_dir.exists():
            stale = list(staging_dir.glob("*.part"))
            for stale_file in stale:
                try:
                    tid = stale_file.stem
                    is_protected = False
                    info = self.db.get_interrupted_transfer(tid)
                    if info and info.expire_at > int(time.time() * 1000):
                        is_protected = True

                    if not is_protected:
                        stale_file.unlink()
                        logger.info("Removed stale staging file: %s", stale_file.name)
                    else:
                        logger.info("Preserved interrupted staging file: %s", stale_file.name)
                except Exception as exc:
                    logger.warning("Could not process staging file %s: %s", stale_file.name, exc)

        # Start mDNS discovery
        await self.discovery.start()
        # Register IPC server as a discovery listener so UI clients receive device updates
        self.discovery.add_listener(self.ipc_server.on_devices_changed)

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

        # Start IPC server for local UI clients
        await self.ipc_server.start()

        # Register IPC callbacks for session/transfer events
        self.add_session_listener(self.ipc_server.on_session_changed)
        self.add_transfer_request_listener(self.ipc_server.on_transfer_request)
        self.add_transfer_progress_listener(self.ipc_server.on_transfer_progress)
        self.add_transfer_complete_listener(self.ipc_server.on_transfer_complete)

        # Start chunk stall monitor
        stall_task = asyncio.create_task(self._monitor_stall_timeouts())
        self._active_tasks.add(stall_task)
        stall_task.add_done_callback(self._active_tasks.discard)

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

        # Stop IPC server
        await self.ipc_server.stop()

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
                "is_paired": False,
            }

            if is_initiator:
                # If we are initiating a connection to a known peer, we might already know their public key
                # Wait, ps.remote_static_pub_b64 is set when connecting from UI for known peers? Let's assume False for now unless we know it.
                if ps.remote_static_pub_b64:
                    existing = self.db.get_device_by_public_key(ps.remote_static_pub_b64)
                    if existing:
                        init_payload["is_paired"] = True

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
                
                # Check if we know this peer before sending response
                remote_static_b64 = remote_payload.get("public_key", "")
                existing = self.db.get_device_by_public_key(remote_static_b64)
                if existing:
                    init_payload["is_paired"] = True
                
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

            # Transition to AUTHENTICATING to verify signatures first
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

            # Check if peer is already trusted
            existing = self.db.get_device_by_public_key(remote_static_b64)
            is_paired = existing is not None
            now = int(time.time() * 1000)

            if is_paired:
                # Update DB with potentially new device_id or device_name from peer
                new_device_id = ps.remote_device_id or existing.device_id
                new_device_name = ps.remote_device_name or existing.device_name
                self.db.add_or_update_device(TrustedDevice(
                    device_id=new_device_id,
                    device_name=new_device_name,
                    public_key=existing.public_key,
                    identity_public_key_b64=existing.identity_public_key_b64,
                    paired_at=existing.paired_at,
                    last_seen=now,
                ))
                session.transition(SessionState.ESTABLISHED)
                self._active_sessions[ps.remote_addr] = ps
                self._notify_session_change(ps.remote_addr, SessionState.ESTABLISHED)
                logger.info("Session ESTABLISHED with %s (%s)", ps.remote_device_name, ps.remote_addr)

                # Phase 5: advertise our capabilities to the peer
                try:
                    await self.send_encrypted(ps, MessageType.CAPABILITIES, {"capabilities": ["notify.v1"]})
                except Exception as _cap_exc:
                    logger.debug("Failed to send CAPABILITIES: %s", _cap_exc)

                if handshake_done is not None:
                    handshake_done.set()
                await self._run_session_loop(ps)
            else:
                session.transition(SessionState.PAIRING)
                self._active_sessions[ps.remote_addr] = ps
                self._notify_session_change(ps.remote_addr, SessionState.PAIRING)
                logger.info("New peer %s — Requesting pairing, SAS: %s", ps.remote_device_name, sas)
                # Broadcast pairing request to any connected UI clients
                self.ipc_server.on_pairing_request(
                    ps.remote_addr,
                    ps.remote_device_name or ps.remote_addr,
                    sas or "",
                )

                # Signal handshake done so the connection setup completes, but loop handles pairing
                if handshake_done is not None:
                    handshake_done.set()
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

                # TRANSFER_CHUNK frames use binary framing (FYCH magic), not JSON
                if is_chunk_frame(plaintext):
                    await self._on_chunk_received(plaintext)
                    continue

                envelope = FerryEnvelope.from_json(plaintext.decode("utf-8"))

                if envelope.type == MessageType.DISCONNECT:
                    logger.info("Peer %s sent DISCONNECT", ps.remote_addr)
                    break
                    
                if envelope.type == MessageType.PAIR_DECISION:
                    decision = envelope.payload.get("decision")
                    logger.info("Received PAIR_DECISION: %s from %s", decision, ps.remote_device_name)
                    if decision == "REJECT":
                        logger.warning("Peer %s rejected pairing", ps.remote_device_name)
                        break
                    if decision == "ACCEPT":
                        if ps.session.state == SessionState.PAIRING:
                            ps.session.transition(SessionState.WAITING_FOR_LOCAL_DECISION)
                        elif ps.session.state == SessionState.WAITING_FOR_REMOTE_DECISION:
                            ps.session.transition(SessionState.PAIR_ACCEPTED)
                            self._persist_trust(ps)
                            ps.session.transition(SessionState.ESTABLISHED)
                            try:
                                await self.send_encrypted(ps, MessageType.CAPABILITIES, {"capabilities": ["notify.v1"]})
                            except Exception as exc:
                                logger.debug("Failed to send CAPABILITIES after pairing: %s", exc)
                        self._notify_session_change(ps.remote_addr, ps.session.state)
                    continue

                # ── Capability exchange ─────────────────────────────────────
                if envelope.type == MessageType.CAPABILITIES:
                    caps = envelope.payload.get("capabilities", [])
                    logger.debug(
                        "Peer %s advertises capabilities: %s", ps.remote_addr, caps
                    )
                    ps.session.peer_capabilities = list(caps)
                    continue

                # ── Notification mirroring (Phase 5) ────────────────────────
                if envelope.type == MessageType.NOTIFICATION_POST:
                    raw = json.dumps(envelope.payload).encode()
                    if len(raw) > MAX_NOTIFICATION_FRAME_BYTES:
                        logger.warning(
                            "NOTIFICATION_POST from %s exceeds 8 KB limit — dropping",
                            ps.remote_addr,
                        )
                        continue
                    try:
                        dto = NotificationPostPayload.from_dict(envelope.payload)
                        self.notification_bridge.post(
                            ferry_id=dto.ferry_id,
                            app_label=dto.app_label,
                            title=dto.title,
                            body=dto.body,
                        )
                    except (ValueError, KeyError) as exc:
                        logger.warning(
                            "Malformed NOTIFICATION_POST from %s: %s", ps.remote_addr, exc
                        )
                    continue

                if envelope.type == MessageType.NOTIFICATION_REMOVE:
                    try:
                        dto = NotificationRemovePayload.from_dict(envelope.payload)
                        self.notification_bridge.remove(dto.ferry_id)
                    except (ValueError, KeyError) as exc:
                        logger.warning(
                            "Malformed NOTIFICATION_REMOVE from %s: %s", ps.remote_addr, exc
                        )
                    continue

                logger.debug("Received encrypted message type=%s from %s", envelope.type, ps.remote_addr)
                await self._handle_transfer_message(ps, envelope)

        except asyncio.TimeoutError:
            logger.info("Session idle timeout with %s", ps.remote_addr)
        except Exception as exc:
            logger.error("Session loop error with %s: %s", ps.remote_addr, exc)
        finally:
            # Phase 3D: Cancel all active transfers for this peer before closing
            self._cancel_incoming_transfer_for_peer(ps)
            self._cancel_outgoing_transfer_for_peer(ps)
            ps.session.transition(SessionState.CLOSING)
            ps.session.transition(SessionState.DISCONNECTED)

    async def _on_chunk_received(self, plaintext: bytes) -> None:
        """Receiver side: process a binary TRANSFER_CHUNK frame."""
        try:
            chunk = decode_chunk_frame(plaintext)
        except ValueError as exc:
            logger.error("Malformed TRANSFER_CHUNK: %s", exc)
            return

        incoming = self._incoming_transfers.get(chunk.transfer_id)
        if not incoming:
            logger.warning(
                "TRANSFER_CHUNK for unknown transfer_id %s (seq=%d)",
                chunk.transfer_id[:8], chunk.seq,
            )
            return

        # Phase 3D: chunk stall timeout reset
        self._last_chunk_time[chunk.transfer_id] = time.monotonic()

        try:
            incoming.receive_chunk(chunk)
            batch_id = incoming.meta.batch_id
            if batch_id:
                batch = self._active_batches.get(batch_id)
                if batch:
                    batch.bytes_transferred += len(chunk.data)
                    self._notify_transfer_progress(batch_id, batch.bytes_transferred, batch.total_bytes)
            else:
                self._notify_transfer_progress(
                    chunk.transfer_id,
                    incoming.bytes_received,
                    incoming.meta.file_size,
                )
        except TransferError as exc:
            logger.error(
                "Disk I/O error for incoming transfer %s: %s",
                chunk.transfer_id[:8], exc,
            )
            ps = self._incoming_peers.pop(chunk.transfer_id, None)
            self._incoming_transfers.pop(chunk.transfer_id, None)
            self._last_chunk_time.pop(chunk.transfer_id, None)
            timeout_task = self._accept_timeout_tasks.pop(chunk.transfer_id, None)
            if timeout_task:
                timeout_task.cancel()
            # Send TRANSFER_ERROR to the sender
            if ps:
                try:
                    await self.send_encrypted(
                        ps, MessageType.TRANSFER_ERROR,
                        {
                            "transfer_id": chunk.transfer_id,
                            "error_code": "ERR_IO_FAILURE",
                            "message": str(exc),
                        }
                    )
                except Exception:
                    pass
                device_id = ps.remote_device_id or ps.remote_addr
                self._record_transfer_once(
                    transfer_id=chunk.transfer_id, device_id=device_id,
                    file_name=incoming.meta.file_name, file_size=incoming.meta.file_size,
                    direction="INCOMING", status="FAILED",
                    started_at=incoming.meta.created_at, sha256="",
                )
            self._notify_transfer_complete(chunk.transfer_id, False, incoming.meta.file_name, "INCOMING")
        except (ValueError, RuntimeError) as exc:
            logger.error(
                "Error processing chunk seq=%d for transfer %s: %s",
                chunk.seq, chunk.transfer_id[:8], exc,
            )
            incoming.cancel()
            self._incoming_transfers.pop(chunk.transfer_id, None)
            self._last_chunk_time.pop(chunk.transfer_id, None)

    async def _handle_transfer_message(self, ps: "PeerSession", envelope: FerryEnvelope) -> None:
        """Dispatch a decrypted transfer control message to the appropriate handler."""
        msg_type = envelope.type
        payload = envelope.payload
        transfer_id = payload.get("transfer_id", "")

        if msg_type == MessageType.TRANSFER_REQUEST:
            await self._on_transfer_request(ps, payload)

        elif msg_type == MessageType.TRANSFER_ACCEPT:
            # Unblock send_file() waiting for accept
            self._signal_transfer_event(ps, "_transfer_events", transfer_id, "ACCEPT")

        elif msg_type == MessageType.TRANSFER_REJECT:
            self._signal_transfer_event(ps, "_transfer_events", transfer_id, "REJECT")

        elif msg_type == MessageType.TRANSFER_COMPLETE:
            await self._on_transfer_complete(ps, transfer_id)

        elif msg_type == MessageType.TRANSFER_RESULT:
            success = bool(payload.get("success", False))
            self._signal_transfer_event(ps, "_result_events", transfer_id, success)

        elif msg_type == MessageType.TRANSFER_CANCEL:
            await self._on_transfer_cancel(ps, transfer_id, payload.get("reason", ""))

        elif msg_type == MessageType.TRANSFER_ERROR:
            logger.error(
                "Transfer error from %s: [%s] %s",
                ps.remote_addr,
                payload.get("error_code", ""),
                payload.get("message", ""),
            )
            self._cancel_incoming_transfer(transfer_id)

        # ── Phase 3E Resume Handlers ──
        elif msg_type == MessageType.TRANSFER_RESUME_REQUEST:
            await self._on_transfer_resume_request(ps, payload)
            
        elif msg_type == MessageType.TRANSFER_RESUME_ACCEPT:
            self._signal_transfer_event(ps, "_transfer_events", transfer_id, "ACCEPT")
            
        elif msg_type == MessageType.TRANSFER_RESUME_REJECT:
            self._signal_transfer_event(ps, "_transfer_events", transfer_id, "REJECT")

        # ── Phase 4C Batch Handlers ──
        elif msg_type == MessageType.BATCH_REQUEST:
            await self._on_batch_request(ps, payload)
            
        elif msg_type == MessageType.BATCH_ACCEPT:
            self._signal_transfer_event(ps, "_transfer_events", payload.get("batch_id", ""), "ACCEPT")

        elif msg_type == MessageType.BATCH_REJECT:
            self._signal_transfer_event(ps, "_transfer_events", payload.get("batch_id", ""), "REJECT")
            
        elif msg_type == MessageType.BATCH_CANCEL:
            await self._on_batch_cancel(ps, payload.get("batch_id", ""))

        elif msg_type == MessageType.BATCH_COMPLETE:
            await self._on_batch_complete(ps, payload.get("batch_id", ""))


        else:
            logger.warning("Unhandled message type %s from %s", msg_type, ps.remote_addr)

    async def accept_transfer(self, remote_addr: str, transfer_id: str) -> None:
        incoming = self._incoming_transfers.get(transfer_id)
        if not incoming:
            batch = self._active_batches.get(transfer_id)
            if batch:
                await self.accept_batch(remote_addr, transfer_id)
            return
        ps = self._active_sessions.get(remote_addr)
        if not ps:
            return
        # Cancel acceptance timeout since user responded
        timeout_task = self._accept_timeout_tasks.pop(transfer_id, None)
        if timeout_task:
            timeout_task.cancel()
        incoming.begin()
        await self.send_encrypted(ps, MessageType.TRANSFER_ACCEPT, {"transfer_id": transfer_id})

    async def reject_transfer(self, remote_addr: str, transfer_id: str) -> None:
        incoming = self._incoming_transfers.pop(transfer_id, None)
        if not incoming:
            batch = self._active_batches.get(transfer_id)
            if batch:
                await self.reject_batch(remote_addr, transfer_id)
            return
        ps = self._active_sessions.get(remote_addr)
        if not ps:
            return
        # Cancel acceptance timeout since user responded
        timeout_task = self._accept_timeout_tasks.pop(transfer_id, None)
        if timeout_task:
            timeout_task.cancel()
        incoming.cancel()
        await self.send_encrypted(ps, MessageType.TRANSFER_REJECT, {"transfer_id": transfer_id, "reason": "USER_REJECTED"})

    def _signal_transfer_event(self, ps: "PeerSession", attr: str, transfer_id: str, value) -> None:
        events = getattr(ps, attr, {})
        entry = events.get(transfer_id)
        if entry:
            event, holder = entry
            holder.append(value)
            event.set()

    async def request_resume(self, remote_addr: str, transfer_id: str) -> bool:
        """Receiver initiates a resume for an interrupted transfer."""
        ps = self._active_sessions.get(remote_addr)
        if not ps:
            logger.error("Cannot resume %s: peer %s not connected", transfer_id[:8], remote_addr)
            return False
            
        info = self.db.get_interrupted_transfer(transfer_id)
        if not info:
            logger.error("Cannot resume %s: no interrupted record found", transfer_id[:8])
            return False
            
        # Re-create IncomingTransfer
        import json as _json
        try:
            meta = TransferMetadata.from_dict(_json.loads(info.original_metadata_json))
        except Exception as exc:
            logger.error("Failed to parse original metadata: %s", exc)
            return False
            
        staging_dir = Path(self.config.download_dir) / "staging"
        incoming = IncomingTransfer(meta=meta, download_dir=Path(self.config.download_dir))
        incoming._temp_path = staging_dir / f"{meta.transfer_id}.part"
        incoming._state = TransferState.INTERRUPTED
        
        try:
            req_payload = incoming.prepare_resume_request(
                expected_bytes=info.bytes_received,
                expected_chunk_index=info.resume_chunk_index
            )
        except Exception as exc:
            logger.error("Failed to prepare resume request for %s: %s", transfer_id[:8], exc)
            return False
            
        self._incoming_transfers[transfer_id] = incoming
        self._incoming_peers[transfer_id] = ps
        
        await self.send_encrypted(ps, MessageType.TRANSFER_RESUME_REQUEST, req_payload.__dict__)
        
        logger.info("TRANSFER_RESUME_REQUEST sent for %s", transfer_id[:8])
        
        # Wait for accept or reject
        try:
            response = await asyncio.wait_for(
                self._wait_for_transfer_response(ps, transfer_id),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            response = "REJECT"
        
        if response != "ACCEPT":
            logger.warning("Resume for %s was rejected", transfer_id[:8])
            self._incoming_transfers.pop(transfer_id, None)
            self._incoming_peers.pop(transfer_id, None)
            return False
            
        try:
            incoming.resume(req_payload.resume_chunk_index)
        except Exception as exc:
            logger.error("Failed to execute resume() for %s: %s", transfer_id[:8], exc)
            self._incoming_transfers.pop(transfer_id, None)
            self._incoming_peers.pop(transfer_id, None)
            return False
            
        logger.info("Resume for %s accepted. Waiting for chunks.", transfer_id[:8])
        return True

    async def _on_batch_request(self, ps: "PeerSession", payload: dict) -> None:
        try:
            req = BatchRequestPayload.from_dict(payload)
        except Exception as exc:
            logger.error("Invalid BATCH_REQUEST from %s: %s", ps.remote_addr, exc)
            return

        # If already busy, reject
        if self._active_batches or self._incoming_transfers:
            logger.warning("Busy — rejecting batch %s from %s", req.batch_id[:8], ps.remote_addr)
            await self.send_encrypted(ps, MessageType.BATCH_REJECT, {"batch_id": req.batch_id, "reason": "BUSY"})
            return

        batch = BatchIncomingTransfer(req.batch_id, req.batch_name, req.total_items, req.total_bytes)
        self._active_batches[req.batch_id] = batch

        logger.info(f"BATCH_REQUEST pending approval for {req.batch_name} from {ps.remote_addr}")
        # Notify UI through the same request listener but with a special flag/format, or use a new listener?
        # To avoid breaking existing UI, maybe we can just fire transfer_request listener?
        # But we need UI to show "Batch". For MVP, let's fire the existing listener but append " (Batch)" to filename.
        for listener in self._transfer_request_listeners:
            try:
                listener(ps.remote_addr, req.batch_id, f"[Batch] {req.batch_name}", req.total_bytes)
            except Exception as exc:
                logger.error("Transfer listener error: %s", exc)

    async def _on_batch_complete(self, ps: "PeerSession", batch_id: str) -> None:
        batch = self._active_batches.pop(batch_id, None)
        if batch:
            logger.info("Batch %s complete", batch_id[:8])

    async def _on_batch_cancel(self, ps: "PeerSession", batch_id: str) -> None:
        batch = self._active_batches.pop(batch_id, None)
        if batch:
            logger.info("Batch %s cancelled", batch_id[:8])
            self._notify_transfer_complete(batch_id, False, batch.batch_name, "INCOMING")

    async def accept_batch(self, remote_addr: str, batch_id: str) -> None:
        batch = self._active_batches.get(batch_id)
        if not batch:
            return
        ps = self._active_sessions.get(remote_addr)
        if not ps:
            return
        batch.state = BatchState.ACCEPTED
        await self.send_encrypted(ps, MessageType.BATCH_ACCEPT, {"batch_id": batch_id})

    async def reject_batch(self, remote_addr: str, batch_id: str) -> None:
        batch = self._active_batches.pop(batch_id, None)
        if not batch:
            return
        ps = self._active_sessions.get(remote_addr)
        if not ps:
            return
        await self.send_encrypted(ps, MessageType.BATCH_REJECT, {"batch_id": batch_id, "reason": "USER_REJECTED"})

    async def _on_transfer_resume_request(self, ps: "PeerSession", payload: dict) -> None:
        """Sender side: handle an incoming TRANSFER_RESUME_REQUEST from the receiver.

        If Linux was the original receiver (incoming transfer interrupted), we have a
        persisted record in the database and can accept the resume after verification.
        If Linux was the original sender (outgoing transfer interrupted), we have no
        record and must reject.
        """
        transfer_id = payload.get("transfer_id")
        if not transfer_id:
            return

        # Check if we have an interrupted incoming transfer record for this transfer_id
        info = self.db.get_interrupted_transfer(transfer_id)
        if not info:
            # No interrupted record — Linux was the sender, cannot resume
            try:
                await self.send_encrypted(
                    ps, MessageType.TRANSFER_RESUME_REJECT,
                    {"transfer_id": transfer_id, "reason": "TRANSFER_NOT_FOUND"}
                )
                logger.info("Rejected resume for %s (no interrupted record — Linux was sender)", transfer_id[:8])
            except Exception as exc:
                logger.warning("Failed to send TRANSFER_RESUME_REJECT: %s", exc)
            return

        # Verify the peer identity matches the original sender
        if ps.remote_static_pub_b64 != info.sender_identity:
            try:
                await self.send_encrypted(
                    ps, MessageType.TRANSFER_RESUME_REJECT,
                    {"transfer_id": transfer_id, "reason": "WRONG_PEER"}
                )
                logger.warning("Rejected resume for %s (peer identity mismatch)", transfer_id[:8])
            except Exception as exc:
                logger.warning("Failed to send TRANSFER_RESUME_REJECT: %s", exc)
            return

        # Verify the partial SHA-256 matches what we have on disk
        request_partial_sha256 = payload.get("partial_sha256", "").lower()
        if request_partial_sha256 != info.partial_sha256.lower():
            try:
                await self.send_encrypted(
                    ps, MessageType.TRANSFER_RESUME_REJECT,
                    {"transfer_id": transfer_id, "reason": "PARTIAL_CORRUPT"}
                )
                logger.warning("Rejected resume for %s (partial SHA-256 mismatch)", transfer_id[:8])
            except Exception as exc:
                logger.warning("Failed to send TRANSFER_RESUME_REJECT: %s", exc)
            return

        # Verify the resume_chunk_index matches
        request_chunk_index = payload.get("resume_chunk_index", 0)
        if request_chunk_index != info.resume_chunk_index:
            try:
                await self.send_encrypted(
                    ps, MessageType.TRANSFER_RESUME_REJECT,
                    {"transfer_id": transfer_id, "reason": "PARTIAL_CORRUPT"}
                )
                logger.warning("Rejected resume for %s (chunk index mismatch)", transfer_id[:8])
            except Exception as exc:
                logger.warning("Failed to send TRANSFER_RESUME_REJECT: %s", exc)
            return

        # All checks passed — accept the resume
        try:
            # Re-create the IncomingTransfer object from persisted metadata
            import json as _json
            meta = TransferMetadata.from_dict(_json.loads(info.original_metadata_json))
            staging_dir = Path(self.config.download_dir) / "staging"
            incoming = IncomingTransfer(meta=meta, download_dir=Path(self.config.download_dir))
            incoming._temp_path = staging_dir / f"{meta.transfer_id}.part"
            incoming._state = TransferState.INTERRUPTED

            # Transition to RESUME_REQUESTED then RESUMING
            incoming.resume(info.resume_chunk_index)

            # Track the incoming transfer for chunk processing
            self._incoming_transfers[transfer_id] = incoming
            self._incoming_peers[transfer_id] = ps

            await self.send_encrypted(
                ps, MessageType.TRANSFER_RESUME_ACCEPT,
                {
                    "transfer_id": transfer_id,
                    "resume_chunk_index": info.resume_chunk_index,
                    "protocol_version": 1,
                }
            )
            logger.info("Accepted resume for %s at chunk %d", transfer_id[:8], info.resume_chunk_index)
        except Exception as exc:
            logger.error("Failed to accept resume for %s: %s", transfer_id[:8], exc)
            try:
                await self.send_encrypted(
                    ps, MessageType.TRANSFER_RESUME_REJECT,
                    {"transfer_id": transfer_id, "reason": "SOURCE_MODIFIED"}
                )
            except Exception:
                pass

    async def _on_transfer_request(self, ps: "PeerSession", payload: dict) -> None:
        """Receiver side: handle an incoming TRANSFER_REQUEST."""
        try:
            meta = TransferMetadata.from_dict(payload)
        except (ValueError, KeyError) as exc:
            logger.error("Invalid TRANSFER_REQUEST from %s: %s", ps.remote_addr, exc)
            await self.send_encrypted(
                ps, MessageType.TRANSFER_ERROR,
                {"transfer_id": payload.get("transfer_id", ""),
                 "error_code": "INVALID_REQUEST", "message": str(exc)}
            )
            return

        # Check if it belongs to an accepted batch
        is_batched = False
        if meta.batch_id:
            batch = self._active_batches.get(meta.batch_id)
            if batch and batch.state == BatchState.ACCEPTED:
                is_batched = True
            else:
                logger.warning("Rejecting transfer %s - invalid or unaccepted batch %s", meta.transfer_id[:8], meta.batch_id[:8])
                await self.send_encrypted(
                    ps, MessageType.TRANSFER_REJECT,
                    {"transfer_id": meta.transfer_id, "reason": "INVALID_REQUEST"}
                )
                return

        # Reject if already handling a single transfer (and it's not a batched one)
        if self._incoming_transfers and not is_batched:
            logger.warning("Busy — rejecting transfer %s from %s", meta.transfer_id[:8], ps.remote_addr)
            await self.send_encrypted(
                ps, MessageType.TRANSFER_REJECT,
                {"transfer_id": meta.transfer_id, "reason": "BUSY"}
            )
            return

        download_dir = Path(self.config.download_dir)
        incoming = IncomingTransfer(meta=meta, download_dir=download_dir)
        self._incoming_transfers[meta.transfer_id] = incoming
        self._incoming_peers[meta.transfer_id] = ps

        logger.info(f"TRANSFER_REQUEST pending approval for {meta.file_name} from {ps.remote_addr}")
        
        if is_batched:
            # Auto-accept since the batch was already approved!
            incoming.begin()
            await self.send_encrypted(ps, MessageType.TRANSFER_ACCEPT, {"transfer_id": meta.transfer_id})
            return

        for listener in self._transfer_request_listeners:
            try:
                listener(ps.remote_addr, meta.transfer_id, meta.file_name, meta.file_size)
            except Exception as exc:
                logger.error("Transfer listener error: %s", exc)
        # Desktop notification for incoming transfer
        self.notifications.incoming_transfer_request(
            ps.remote_device_name or ps.remote_addr,
            meta.file_name,
            meta.file_size,
        )
        logger.info(
            "TRANSFER_REQUEST notified UI for %s from %s",
            meta.file_name, ps.remote_addr,
        )

        # Phase 3D: Start acceptance timeout — if the user doesn't respond in time,
        # auto-reject the transfer to free resources.
        async def _acceptance_timeout(transfer_id: str, timeout: float) -> None:
            await asyncio.sleep(timeout)
            if transfer_id not in self._incoming_transfers:
                return  # Already resolved (accepted or rejected)
            logger.warning(
                "Acceptance timeout for transfer %s — auto-rejecting",
                transfer_id[:8],
            )
            still_pending = self._incoming_transfers.pop(transfer_id, None)
            self._incoming_peers.pop(transfer_id, None)
            self._accept_timeout_tasks.pop(transfer_id, None)
            if still_pending:
                still_pending.cancel()
                try:
                    await self.send_encrypted(
                        ps, MessageType.TRANSFER_REJECT,
                        {"transfer_id": transfer_id, "reason": "TIMEOUT_EXPIRED"}
                    )
                except Exception:
                    pass
                self._record_transfer_once(
                    transfer_id=transfer_id,
                    device_id=ps.remote_device_id or ps.remote_addr,
                    file_name=meta.file_name, file_size=meta.file_size,
                    direction="INCOMING", status="REJECTED",
                    started_at=meta.created_at, sha256="",
                )
                self._notify_transfer_complete(transfer_id, False, meta.file_name, "INCOMING")

        timeout_task = asyncio.create_task(
            _acceptance_timeout(meta.transfer_id, TRANSFER_ACCEPT_TIMEOUT_SECS)
        )
        self._active_tasks.add(timeout_task)
        timeout_task.add_done_callback(self._active_tasks.discard)
        self._accept_timeout_tasks[meta.transfer_id] = timeout_task

    async def _on_transfer_complete(self, ps: "PeerSession", transfer_id: str) -> None:
        """Receiver side: sender says all chunks sent — verify integrity."""
        incoming = self._incoming_transfers.get(transfer_id)
        if not incoming:
            # Phase 3D: Duplicate TRANSFER_COMPLETE — already finalised; ignore.
            logger.warning(
                "TRANSFER_COMPLETE for unknown or already-finalised transfer_id %s — ignoring",
                transfer_id[:8] if transfer_id else "?",
            )
            return

        # Cancel acceptance timeout (if somehow still running)
        timeout_task = self._accept_timeout_tasks.pop(transfer_id, None)
        if timeout_task:
            timeout_task.cancel()
        self._last_chunk_time.pop(transfer_id, None)

        success = incoming.finalise()
        sha256 = incoming.meta.sha256 if success else ""
        await self.send_encrypted(
            ps, MessageType.TRANSFER_RESULT,
            {"transfer_id": transfer_id, "success": success, "sha256": sha256}
        )
        self._incoming_transfers.pop(transfer_id, None)
        self._incoming_peers.pop(transfer_id, None)

        # Persist to transfer history (exactly once via _record_transfer_once)
        device_id = ps.remote_device_id or ps.remote_addr
        batch_id = incoming.meta.batch_id
        if not batch_id:
            self._record_transfer_once(
                transfer_id=transfer_id, device_id=device_id,
                file_name=incoming.meta.file_name, file_size=incoming.meta.file_size,
                direction="INCOMING", status="COMPLETED" if success else "FAILED",
                started_at=incoming.meta.created_at, sha256=sha256,
            )
            self._notify_transfer_complete(
                transfer_id, success, incoming.meta.file_name, "INCOMING"
            )
            # Desktop notification on completion
            self.notifications.transfer_complete(
                incoming.meta.file_name, success, "INCOMING"
            )
        else:
            batch = self._active_batches.get(batch_id)
            if batch:
                if success:
                    batch.items_completed += 1
                else:
                    batch.items_failed += 1
                
                # Check if batch is fully received
                if batch.items_completed + batch.items_failed == batch.total_items:
                    batch_success = batch.items_failed == 0
                    self._active_batches.pop(batch_id, None)
                    status = "COMPLETED" if batch_success else "FAILED"
                    self._record_transfer_once(
                        transfer_id=batch_id, device_id=device_id,
                        file_name=batch.batch_name, file_size=batch.total_bytes,
                        direction="INCOMING", status=status,
                        started_at=incoming.meta.created_at, sha256="",
                    )
                    self._notify_transfer_complete(
                        batch_id, batch_success, batch.batch_name, "INCOMING"
                    )
                    self.notifications.transfer_complete(
                        batch.batch_name, batch_success, "INCOMING"
                    )

    async def _on_transfer_cancel(self, ps: "PeerSession", transfer_id: str, reason: str) -> None:
        logger.info(
            "TRANSFER_CANCEL received for %s from %s (reason=%s)",
            transfer_id[:8] if transfer_id else "?", ps.remote_addr, reason
        )
        # Phase 3D: idempotent cancel — if already terminal, just unblock events
        already_done = (
            transfer_id not in self._incoming_transfers
            and transfer_id not in self._active_outgoing_transfers
        )
        if already_done:
            logger.debug(
                "TRANSFER_CANCEL for already-resolved transfer %s — unblocking events only",
                transfer_id[:8] if transfer_id else "?",
            )
            self._signal_transfer_event(ps, "_transfer_events", transfer_id, "CANCEL")
            self._signal_transfer_event(ps, "_result_events", transfer_id, False)
            return

        # Cancel acceptance timeout if pending
        timeout_task = self._accept_timeout_tasks.pop(transfer_id, None)
        if timeout_task:
            timeout_task.cancel()
        self._last_chunk_time.pop(transfer_id, None)

        # Cancel incoming if active
        incoming = self._incoming_transfers.pop(transfer_id, None)
        self._incoming_peers.pop(transfer_id, None)
        if incoming:
            incoming.cancel()
            device_id = ps.remote_device_id or ps.remote_addr
            self._record_transfer_once(
                transfer_id=transfer_id, device_id=device_id,
                file_name=incoming.meta.file_name, file_size=incoming.meta.file_size,
                direction="INCOMING", status="CANCELLED",
                started_at=incoming.meta.created_at, sha256="",
            )
            self._notify_transfer_complete(transfer_id, False, incoming.meta.file_name, "INCOMING")

        # Cancel outgoing if active
        if transfer_id in self._active_outgoing_transfers:
            _, xfer = self._active_outgoing_transfers[transfer_id]
            xfer.cancel()

        # Unblock any waiting events on peer session
        self._signal_transfer_event(ps, "_transfer_events", transfer_id, "CANCEL")
        self._signal_transfer_event(ps, "_result_events", transfer_id, False)

    def _cancel_incoming_transfer(self, transfer_id: str) -> None:
        incoming = self._incoming_transfers.pop(transfer_id, None)
        self._incoming_peers.pop(transfer_id, None)
        timeout_task = self._accept_timeout_tasks.pop(transfer_id, None)
        if timeout_task:
            timeout_task.cancel()
        self._last_chunk_time.pop(transfer_id, None)
        if incoming:
            incoming.cancel()
            self._notify_transfer_complete(transfer_id, False, incoming.meta.file_name, "INCOMING")

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

    async def _monitor_stall_timeouts(self) -> None:
        """Background task to detect stalled incoming transfers."""
        import time
        while self._is_running:
            await asyncio.sleep(5.0)
            now = time.monotonic()
            stalled_transfers = []
            for tid, last_t in list(self._last_chunk_time.items()):
                if now - last_t > 30.0:  # 30 seconds stall timeout
                    stalled_transfers.append(tid)
            
            for tid in stalled_transfers:
                logger.warning("Transfer %s stalled for over 30s. Disconnecting session.", tid[:8])
                # Find the session to disconnect it, which will cleanly interrupt the transfer
                ps = self._incoming_peers.get(tid)
                if ps:
                    try:
                        ps.writer.close()
                    except Exception:
                        pass

    async def send_encrypted(self, ps: PeerSession, msg_type: MessageType, payload: dict) -> None:
        """Send an encrypted Ferry envelope to an established or pairing session peer."""
        if ps.state in (SessionState.DISCONNECTED, SessionState.CONNECTING, SessionState.HANDSHAKING, SessionState.FAILED, SessionState.CLOSING):
            raise RuntimeError(f"Cannot send encrypted to session in state {ps.state}")
        envelope = FerryEnvelope(type=msg_type.value, payload=payload)
        plaintext = envelope.to_json().encode("utf-8")
        frame = ps.session.encrypt_frame(plaintext)
        ps.writer.write(frame)
        await ps.writer.drain()
