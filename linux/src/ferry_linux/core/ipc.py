"""
Ferry IPC Server — UNIX Domain Socket interface between the daemon and UI clients.

Protocol: same FY-magic + 4-byte length + JSON framing used on the wire, but
without AEAD (local socket, mode 0600, trusted by OS user-ID).

The server:
  - Creates $XDG_RUNTIME_DIR/ferry.sock on start()
  - Accepts multiple concurrent UI clients
  - Pushes events to all connected clients as they happen
  - Dispatches UI commands to FerryService methods

Message types (IPC-only, never sent to remote Android peers):

  D → UI  (daemon → client)
  ─────────────────────────
  IPC_STATE_SNAPSHOT       Full state dump: devices, sessions, transfers, config
  IPC_DEVICE_UPDATE        Discovery or trust list changed
  IPC_SESSION_UPDATE       Session state changed for a peer
  IPC_TRANSFER_REQUEST     Incoming transfer needs user accept/reject
  IPC_TRANSFER_PROGRESS    Transfer progress tick
  IPC_TRANSFER_COMPLETE    Transfer finished (success/failure)
  IPC_PAIRING_REQUEST      New pairing: SAS code display needed
  IPC_CLIPBOARD_INCOMING   Clipboard text received from remote peer
  IPC_NOTIFICATION         General notification text
  IPC_ERROR                Error response to a UI command

  UI → D  (client → daemon)
  ─────────────────────────
  IPC_SUBSCRIBE            Client connects and requests state + events
  IPC_ACCEPT_TRANSFER      User accepted incoming transfer
  IPC_REJECT_TRANSFER      User rejected incoming transfer
  IPC_CANCEL_TRANSFER      User cancelled active transfer
  IPC_ACCEPT_BATCH         User accepted incoming batch
  IPC_REJECT_BATCH         User rejected incoming batch
  IPC_ACCEPT_PAIRING       User confirmed SAS match
  IPC_REJECT_PAIRING       User rejected pairing
  IPC_SEND_FILE            Send file to a peer
  IPC_SEND_BATCH           Send multiple files/folder to a peer
  IPC_CONNECT_PEER         Connect to a discovered device
  IPC_GET_HISTORY          Request transfer history
  IPC_DISCARD_RESUME       Discard an interrupted transfer
  IPC_REQUEST_RESUME       Resume an interrupted transfer
  IPC_CLIPBOARD_SEND       Send clipboard text to peer
  IPC_GET_TRUSTED_DEVICES  Request trusted device list
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .service import FerryService

logger = logging.getLogger("ferry.ipc")

# Frame framing constants (same as wire protocol)
IPC_MAGIC = b"FY"
IPC_MAX_FRAME = 8 * 1024 * 1024  # 8 MiB (larger than wire limit; local only)

# IPC message type constants
class IPCMessageType:
    # Daemon → UI
    STATE_SNAPSHOT      = "IPC_STATE_SNAPSHOT"
    DEVICE_UPDATE       = "IPC_DEVICE_UPDATE"
    SESSION_UPDATE      = "IPC_SESSION_UPDATE"
    TRANSFER_REQUEST    = "IPC_TRANSFER_REQUEST"
    TRANSFER_PROGRESS   = "IPC_TRANSFER_PROGRESS"
    TRANSFER_COMPLETE   = "IPC_TRANSFER_COMPLETE"
    PAIRING_REQUEST     = "IPC_PAIRING_REQUEST"
    CLIPBOARD_INCOMING  = "IPC_CLIPBOARD_INCOMING"
    NOTIFICATION        = "IPC_NOTIFICATION"
    ERROR               = "IPC_ERROR"
    HISTORY_RESULT      = "IPC_HISTORY_RESULT"
    TRUSTED_DEVICES_RESULT = "IPC_TRUSTED_DEVICES_RESULT"

    # UI → Daemon
    SUBSCRIBE           = "IPC_SUBSCRIBE"
    ACCEPT_TRANSFER     = "IPC_ACCEPT_TRANSFER"
    REJECT_TRANSFER     = "IPC_REJECT_TRANSFER"
    CANCEL_TRANSFER     = "IPC_CANCEL_TRANSFER"
    ACCEPT_BATCH        = "IPC_ACCEPT_BATCH"
    REJECT_BATCH        = "IPC_REJECT_BATCH"
    ACCEPT_PAIRING      = "IPC_ACCEPT_PAIRING"
    REJECT_PAIRING      = "IPC_REJECT_PAIRING"
    SEND_FILE           = "IPC_SEND_FILE"
    SEND_BATCH          = "IPC_SEND_BATCH"
    CONNECT_PEER        = "IPC_CONNECT_PEER"
    GET_HISTORY         = "IPC_GET_HISTORY"
    DISCARD_RESUME      = "IPC_DISCARD_RESUME"
    REQUEST_RESUME      = "IPC_REQUEST_RESUME"
    CLIPBOARD_SEND      = "IPC_CLIPBOARD_SEND"
    GET_TRUSTED_DEVICES = "IPC_GET_TRUSTED_DEVICES"
    REMOVE_TRUSTED_DEVICE = "IPC_REMOVE_TRUSTED_DEVICE"
    SET_CLIPBOARD_SYNC  = "IPC_SET_CLIPBOARD_SYNC"

def _encode_ipc_frame(msg_type: str, payload: dict) -> bytes:
    """Encode a local IPC message as an FY-framed JSON envelope."""
    envelope = {
        "type": msg_type,
        "ts": int(time.time() * 1000),
        "payload": payload,
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return IPC_MAGIC + struct.pack("!I", len(body)) + body


async def _read_ipc_frame(reader: asyncio.StreamReader) -> Optional[dict]:
    """Read one FY-framed message from the stream. Returns None on EOF."""
    try:
        header = await reader.readexactly(6)  # 2 magic + 4 length
    except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
        return None
    if header[:2] != IPC_MAGIC:
        logger.error("IPC frame bad magic: %r", header[:2])
        return None
    (length,) = struct.unpack("!I", header[2:])
    if length > IPC_MAX_FRAME:
        logger.error("IPC frame too large: %d bytes", length)
        return None
    try:
        body = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("IPC frame JSON decode error: %s", exc)
        return None


class _IPCClient:
    """One connected UI client."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._closed = False
        peer = writer.get_extra_info("peername")
        self.addr = str(peer) if peer else "<local>"

    async def send(self, msg_type: str, payload: dict) -> None:
        """Send an event to this UI client. Silently drop on broken connection."""
        if self._closed:
            return
        try:
            frame = _encode_ipc_frame(msg_type, payload)
            self.writer.write(frame)
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            self._closed = True
        except Exception as exc:
            logger.debug("IPC send error to %s: %s", self.addr, exc)
            self._closed = True

    def close(self) -> None:
        self._closed = True
        try:
            self.writer.close()
        except Exception:
            pass


class FerryIPCServer:
    """
    UNIX domain socket server that bridges FerryService events to local UI clients.

    Usage:
        server = FerryIPCServer(ferry_service)
        await server.start()
        # ... service runs ...
        await server.stop()
    """

    def __init__(self, service: "FerryService") -> None:
        self._service = service
        self._clients: list[_IPCClient] = []
        self._server: Optional[asyncio.AbstractServer] = None
        self._sock_path: Optional[Path] = None

    @property
    def socket_path(self) -> Optional[Path]:
        return self._sock_path

    async def start(self) -> None:
        """Create and bind the UNIX domain socket."""
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        self._sock_path = Path(runtime_dir) / "ferry.sock"

        # Remove stale socket from a previous crash
        if self._sock_path.exists():
            try:
                self._sock_path.unlink()
            except OSError:
                pass

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._sock_path),
        )
        # Restrict to current user only
        try:
            os.chmod(str(self._sock_path), 0o600)
        except OSError:
            pass
        logger.info("IPC server listening at %s", self._sock_path)

    async def stop(self) -> None:
        """Close the server and all connected clients."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        for client in list(self._clients):
            client.close()
        self._clients.clear()

        if self._sock_path and self._sock_path.exists():
            try:
                self._sock_path.unlink()
            except OSError:
                pass
        logger.info("IPC server stopped")

    # ------------------------------------------------------------------
    # Push events to all connected UI clients
    # ------------------------------------------------------------------

    async def broadcast(self, msg_type: str, payload: dict) -> None:
        """Send a message to all connected UI clients."""
        dead = []
        for client in list(self._clients):
            await client.send(msg_type, payload)
            if client._closed:
                dead.append(client)
        for c in dead:
            if c in self._clients:
                self._clients.remove(c)

    # Convenience broadcast helpers (called from FerryService callbacks)

    def on_devices_changed(self, devices: list) -> None:
        asyncio.ensure_future(self.broadcast(
            IPCMessageType.DEVICE_UPDATE,
            {"devices": [d.to_dict() if hasattr(d, "to_dict") else vars(d) for d in devices]},
        ))

    def on_session_changed(self, remote_addr: str, state) -> None:
        payload = {
            "remote_addr": remote_addr,
            "state": str(state.name if hasattr(state, "name") else state),
        }
        ps = self._service._active_sessions.get(remote_addr)
        if ps:
            payload["remote_device_id"] = ps.remote_device_id
            payload["remote_device_name"] = ps.remote_device_name
            payload["remote_static_pub_b64"] = ps.remote_static_pub_b64

        asyncio.ensure_future(self.broadcast(IPCMessageType.SESSION_UPDATE, payload))

    def on_transfer_request(self, remote_addr: str, transfer_id: str, file_name: str, file_size: int) -> None:
        asyncio.ensure_future(self.broadcast(
            IPCMessageType.TRANSFER_REQUEST,
            {
                "remote_addr": remote_addr,
                "transfer_id": transfer_id,
                "file_name": file_name,
                "file_size": file_size,
            },
        ))

    def on_transfer_progress(self, transfer_id: str, bytes_done: int, total_bytes: int) -> None:
        asyncio.ensure_future(self.broadcast(
            IPCMessageType.TRANSFER_PROGRESS,
            {"transfer_id": transfer_id, "bytes_done": bytes_done, "total_bytes": total_bytes},
        ))

    def on_transfer_complete(self, transfer_id: str, success: bool, file_name: str, direction: str) -> None:
        asyncio.ensure_future(self.broadcast(
            IPCMessageType.TRANSFER_COMPLETE,
            {
                "transfer_id": transfer_id,
                "success": success,
                "file_name": file_name,
                "direction": direction,
            },
        ))

    def on_pairing_request(self, remote_addr: str, device_name: str, sas_code: str) -> None:
        asyncio.ensure_future(self.broadcast(
            IPCMessageType.PAIRING_REQUEST,
            {"remote_addr": remote_addr, "device_name": device_name, "sas_code": sas_code},
        ))

    def on_clipboard_incoming(self, text: str) -> None:
        asyncio.ensure_future(self.broadcast(
            IPCMessageType.CLIPBOARD_INCOMING,
            {"text": text},
        ))

    # ------------------------------------------------------------------
    # Client connection handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        client = _IPCClient(reader, writer)
        logger.info("IPC UI client connected")

        try:
            while True:
                msg = await _read_ipc_frame(reader)
                if msg is None:
                    break

                msg_type = msg.get("type", "")
                payload = msg.get("payload", {})

                if msg_type == IPCMessageType.SUBSCRIBE:
                    self._clients.append(client)
                    await self._send_state_snapshot(client)
                    logger.info("IPC client subscribed")
                    continue

                if client not in self._clients:
                    # Must subscribe before sending commands
                    await client.send(IPCMessageType.ERROR, {"message": "Must send IPC_SUBSCRIBE first"})
                    continue

                await self._dispatch_command(client, msg_type, payload)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("IPC client error: %s", exc)
        finally:
            client.close()
            if client in self._clients:
                self._clients.remove(client)
            logger.info("IPC UI client disconnected")

    async def _send_state_snapshot(self, client: _IPCClient) -> None:
        """Send full current state to a newly connected UI client."""
        svc = self._service

        # Discovered devices
        try:
            devices = [
                {
                    "device_id": d.device_id,
                    "device_name": d.device_name,
                    "device_type": d.device_type,
                    "addresses": list(d.addresses),
                    "port": d.port,
                    "is_trusted": svc.db.is_trusted(d.device_id) if d.device_id else False,
                }
                for d in svc.discovered_devices
            ]
        except Exception:
            devices = []

        # Active sessions
        try:
            sessions = [
                {
                    "remote_addr": addr,
                    "remote_device_id": ps.remote_device_id,
                    "remote_device_name": ps.remote_device_name,
                    "state": ps.state.name,
                }
                for addr, ps in svc._active_sessions.items()
            ]
        except Exception:
            sessions = []

        # Active transfers
        try:
            active_transfers = []
            for tid, incoming in svc._incoming_transfers.items():
                active_transfers.append({
                    "transfer_id": tid,
                    "direction": "INCOMING",
                    "file_name": incoming.meta.file_name,
                    "file_size": incoming.meta.file_size,
                    "bytes_done": incoming.bytes_received,
                    "state": incoming.state.name,
                })
            for tid, (ps, outgoing) in svc._active_outgoing_transfers.items():
                active_transfers.append({
                    "transfer_id": tid,
                    "direction": "OUTGOING",
                    "file_name": outgoing.meta.file_name if outgoing.meta else "",
                    "file_size": outgoing.meta.file_size if outgoing.meta else 0,
                    "state": outgoing.state.name if outgoing.state else "UNKNOWN",
                })
        except Exception:
            active_transfers = []

        # Config
        try:
            config_dict = {
                "device_name": svc.config.device_name,
                "device_id": svc.config.device_id,
                "listen_port": svc.config.listen_port,
                "download_dir": str(svc.config.download_dir),
            }
        except Exception:
            config_dict = {}

        # Identity
        try:
            identity_key_prefix = svc.identity.public_key_b64[:12]
        except Exception:
            identity_key_prefix = ""

        await client.send(IPCMessageType.STATE_SNAPSHOT, {
            "devices": devices,
            "sessions": sessions,
            "active_transfers": active_transfers,
            "config": config_dict,
            "identity_key_prefix": identity_key_prefix,
        })

    async def _dispatch_command(self, client: _IPCClient, msg_type: str, payload: dict) -> None:
        """Dispatch a UI command to FerryService."""
        svc = self._service

        try:
            if msg_type == IPCMessageType.ACCEPT_TRANSFER:
                transfer_id = payload["transfer_id"]
                remote_addr = payload.get("remote_addr", "")
                await svc.accept_transfer(remote_addr, transfer_id)

            elif msg_type == IPCMessageType.REJECT_TRANSFER:
                transfer_id = payload["transfer_id"]
                remote_addr = payload.get("remote_addr", "")
                await svc.reject_transfer(remote_addr, transfer_id)

            elif msg_type == IPCMessageType.CANCEL_TRANSFER:
                transfer_id = payload["transfer_id"]
                reason = payload.get("reason", "USER_CANCELLED")
                await svc.cancel_transfer(transfer_id, reason)

            elif msg_type == IPCMessageType.ACCEPT_BATCH:
                batch_id = payload["batch_id"]
                remote_addr = payload["remote_addr"]
                await svc.accept_batch(remote_addr, batch_id)

            elif msg_type == IPCMessageType.REJECT_BATCH:
                batch_id = payload["batch_id"]
                remote_addr = payload["remote_addr"]
                await svc.reject_batch(remote_addr, batch_id)

            elif msg_type == IPCMessageType.ACCEPT_PAIRING:
                remote_addr = payload["remote_addr"]
                await svc.accept_pairing(remote_addr)

            elif msg_type == IPCMessageType.REJECT_PAIRING:
                remote_addr = payload["remote_addr"]
                await svc.reject_pairing(remote_addr)

            elif msg_type == IPCMessageType.SEND_FILE:
                remote_addr = payload["remote_addr"]
                file_path = Path(payload["file_path"])
                asyncio.ensure_future(svc.send_file(remote_addr, file_path))

            elif msg_type == IPCMessageType.SEND_BATCH:
                remote_addr = payload["remote_addr"]
                paths = [Path(p) for p in payload["paths"]]
                batch_name = payload.get("batch_name", "Batch")
                asyncio.ensure_future(svc.send_batch(remote_addr, paths, batch_name))

            elif msg_type == IPCMessageType.CONNECT_PEER:
                device_id = payload.get("device_id", "")
                # Find device in discovered list by device_id
                target = next(
                    (d for d in svc.discovered_devices if d.device_id == device_id), None
                )
                if target:
                    asyncio.ensure_future(svc.connect_to_peer(target))
                else:
                    await client.send(IPCMessageType.ERROR, {"message": f"Device {device_id} not found"})

            elif msg_type == IPCMessageType.GET_HISTORY:
                limit = payload.get("limit", 50)
                try:
                    records = svc.db.list_transfers(limit=limit)
                    await client.send(IPCMessageType.HISTORY_RESULT, {
                        "records": [
                            {
                                "transfer_id": r.transfer_id,
                                "device_id": r.device_id,
                                "file_name": r.file_name,
                                "file_size": r.file_size,
                                "direction": r.direction,
                                "status": r.status,
                                "started_at": r.started_at,
                                "completed_at": r.completed_at,
                                "sha256": r.sha256,
                            }
                            for r in records
                        ]
                    })
                except Exception as exc:
                    await client.send(IPCMessageType.ERROR, {"message": str(exc)})

            elif msg_type == IPCMessageType.GET_TRUSTED_DEVICES:
                try:
                    devices = svc.db.list_devices()
                    await client.send(IPCMessageType.TRUSTED_DEVICES_RESULT, {
                        "devices": [
                            {
                                "device_id": getattr(d, "device_id", ""),
                                "device_name": getattr(d, "device_name", ""),
                                "identity_public_key_b64": getattr(d, "identity_public_key_b64", ""),
                                "last_seen": getattr(d, "last_seen", 0),
                            }
                            for d in devices
                        ]
                    })
                except Exception as exc:
                    await client.send(IPCMessageType.ERROR, {"message": str(exc)})

            elif msg_type == IPCMessageType.DISCARD_RESUME:
                transfer_id = payload["transfer_id"]
                await svc.discard_interrupted_transfer(transfer_id)

            elif msg_type == IPCMessageType.REQUEST_RESUME:
                remote_addr = payload["remote_addr"]
                transfer_id = payload["transfer_id"]
                asyncio.ensure_future(svc.request_resume(remote_addr, transfer_id))

            elif msg_type == IPCMessageType.CLIPBOARD_SEND:
                text = payload.get("text", "")
                # Send to all established sessions
                for ps in svc._active_sessions.values():
                    from .session import SessionState
                    if ps.state == SessionState.ESTABLISHED:
                        asyncio.ensure_future(svc.send_encrypted(
                            ps,
                            __import__("ferry_linux.protocol.models", fromlist=["MessageType"]).MessageType.CLIPBOARD_SYNC,
                            {"text": text, "ts": int(time.time() * 1000)},
                        ))

            elif msg_type == IPCMessageType.REMOVE_TRUSTED_DEVICE:
                pubkey = payload["public_key_b64"]
                svc.db.remove_device_by_public_key(pubkey)
                asyncio.ensure_future(self.broadcast(
                    IPCMessageType.TRUSTED_DEVICES_RESULT,
                    {
                        "devices": [
                            {
                                "device_id": getattr(d, "device_id", ""),
                                "device_name": getattr(d, "device_name", ""),
                                "identity_public_key_b64": getattr(d, "identity_public_key_b64", ""),
                                "last_seen": getattr(d, "last_seen", 0),
                            }
                            for d in svc.db.list_devices()
                        ]
                    }
                ))

            elif msg_type == IPCMessageType.SET_CLIPBOARD_SYNC:
                enabled = payload["enabled"]
                if hasattr(svc, "clipboard_sync"):
                    svc.clipboard_sync.set_enabled(enabled)

            else:
                logger.warning("Unknown IPC command: %s", msg_type)
                await client.send(IPCMessageType.ERROR, {"message": f"Unknown command: {msg_type}"})

        except KeyError as exc:
            await client.send(IPCMessageType.ERROR, {"message": f"Missing field: {exc}"})
        except Exception as exc:
            logger.error("IPC dispatch error for %s: %s", msg_type, exc)
            await client.send(IPCMessageType.ERROR, {"message": str(exc)})
