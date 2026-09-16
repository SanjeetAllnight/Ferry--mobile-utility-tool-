"""
Ferry IPC Client — connects the GTK UI to the running Ferry daemon.

Connects to $XDG_RUNTIME_DIR/ferry.sock, subscribes to events, and
dispatches UI commands over the same FY-framed JSON protocol used in ipc.py.

The client runs its asyncio loop in a background thread (same as the old
in-process FerryService setup), but all interaction with the daemon is via
the UNIX socket rather than direct method calls.

Thread model:
  - GTK main loop runs on the main thread
  - IPC client event loop runs on a background daemon thread
  - GLib.idle_add() is used to safely push updates from background → GTK
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("ferry.ipc_client")

IPC_MAGIC = b"FY"
IPC_MAX_FRAME = 8 * 1024 * 1024

# Default retry/timeout settings
CONNECT_RETRY_INTERVAL = 1.0  # seconds between connect retries
CONNECT_MAX_RETRIES = 10       # before giving up


class FerryIPCClient:
    """
    Connects to the Ferry daemon's UNIX socket and:
      - Sends IPC_SUBSCRIBE to receive state + events
      - Pushes all events to registered callbacks (via GLib.idle_add)
      - Exposes async send methods for UI commands
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._closing = False

        # Callbacks registered by the UI
        self._on_state_snapshot:   Optional[Callable] = None
        self._on_device_update:    Optional[Callable] = None
        self._on_session_update:   Optional[Callable] = None
        self._on_transfer_request: Optional[Callable] = None
        self._on_transfer_progress: Optional[Callable] = None
        self._on_transfer_complete: Optional[Callable] = None
        self._on_pairing_request:  Optional[Callable] = None
        self._on_clipboard_incoming: Optional[Callable] = None
        self._on_history_result:   Optional[Callable] = None
        self._on_trusted_devices:  Optional[Callable] = None
        self._on_connected:        Optional[Callable] = None
        self._on_disconnected:     Optional[Callable] = None

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on(self, event: str, callback: Callable) -> None:
        """Register a callback for a named event.  All callbacks are called
        on the GTK main thread via GLib.idle_add.
        """
        attr = f"_on_{event}"
        if hasattr(self, attr):
            setattr(self, attr, callback)
        else:
            raise ValueError(f"Unknown IPC client event: {event!r}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background asyncio thread and begin connecting."""
        if self._thread and self._thread.is_alive():
            return
        self._closing = False
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ferry-ipc-client",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the client to disconnect and stop the background thread."""
        self._closing = True
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_and_run())
        except Exception as exc:
            logger.error("IPC client loop error: %s", exc)

    @staticmethod
    def _socket_path() -> str:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        return str(Path(runtime_dir) / "ferry.sock")

    async def _connect_and_run(self) -> None:
        """Connect to the daemon, subscribing on success, retrying on failure."""
        retries = 0
        while not self._closing:
            sock_path = self._socket_path()
            try:
                reader, writer = await asyncio.open_unix_connection(sock_path)
            except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
                retries += 1
                if retries >= CONNECT_MAX_RETRIES:
                    logger.error("IPC: could not connect to daemon at %s after %d retries", sock_path, retries)
                    self._fire("disconnected")
                    return
                logger.debug("IPC: waiting for daemon socket %s (%s)…", sock_path, exc)
                await asyncio.sleep(CONNECT_RETRY_INTERVAL)
                continue

            retries = 0
            self._writer = writer
            self._connected = True
            logger.info("IPC client connected to daemon at %s", sock_path)
            self._fire("connected")

            # Subscribe: request state snapshot + event stream
            await self._send_raw("IPC_SUBSCRIBE", {})

            try:
                await self._read_loop(reader)
            except Exception as exc:
                logger.warning("IPC read loop ended: %s", exc)

            self._connected = False
            self._writer = None
            self._fire("disconnected")

            if not self._closing:
                logger.info("IPC: daemon disconnected, will retry…")
                await asyncio.sleep(CONNECT_RETRY_INTERVAL)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Read and dispatch messages from the daemon."""
        while not self._closing:
            msg = await _read_ipc_frame(reader)
            if msg is None:
                break
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        """Route an incoming IPC message to the appropriate callback."""
        from gi.repository import GLib

        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        dispatch_map = {
            "IPC_STATE_SNAPSHOT":      self._on_state_snapshot,
            "IPC_DEVICE_UPDATE":       self._on_device_update,
            "IPC_SESSION_UPDATE":      self._on_session_update,
            "IPC_TRANSFER_REQUEST":    self._on_transfer_request,
            "IPC_TRANSFER_PROGRESS":   self._on_transfer_progress,
            "IPC_TRANSFER_COMPLETE":   self._on_transfer_complete,
            "IPC_PAIRING_REQUEST":     self._on_pairing_request,
            "IPC_CLIPBOARD_INCOMING":  self._on_clipboard_incoming,
            "IPC_HISTORY_RESULT":      self._on_history_result,
            "IPC_TRUSTED_DEVICES_RESULT": self._on_trusted_devices,
        }
        cb = dispatch_map.get(msg_type)
        if cb:
            # Always call UI callbacks on the GTK main thread
            GLib.idle_add(cb, payload)
        elif msg_type == "IPC_ERROR":
            logger.warning("IPC daemon error: %s", payload.get("message", ""))
        else:
            logger.debug("Unknown IPC message type: %s", msg_type)

    def _fire(self, event: str) -> None:
        from gi.repository import GLib
        cb = getattr(self, f"_on_{event}", None)
        if cb:
            GLib.idle_add(cb)

    # ------------------------------------------------------------------
    # Send helpers (all thread-safe; schedule on the IPC event loop)
    # ------------------------------------------------------------------

    def _schedule(self, coro) -> None:
        """Schedule a coroutine on the IPC event loop (from any thread)."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _send_raw(self, msg_type: str, payload: dict) -> None:
        if self._writer is None:
            logger.warning("IPC: cannot send %s — not connected", msg_type)
            return
        try:
            frame = _encode_ipc_frame(msg_type, payload)
            self._writer.write(frame)
            await self._writer.drain()
        except Exception as exc:
            logger.warning("IPC send error (%s): %s", msg_type, exc)

    # Public command methods (callable from GTK thread)

    def accept_transfer(self, remote_addr: str, transfer_id: str) -> None:
        self._schedule(self._send_raw("IPC_ACCEPT_TRANSFER", {
            "remote_addr": remote_addr, "transfer_id": transfer_id
        }))

    def reject_transfer(self, remote_addr: str, transfer_id: str) -> None:
        self._schedule(self._send_raw("IPC_REJECT_TRANSFER", {
            "remote_addr": remote_addr, "transfer_id": transfer_id
        }))

    def cancel_transfer(self, transfer_id: str) -> None:
        self._schedule(self._send_raw("IPC_CANCEL_TRANSFER", {"transfer_id": transfer_id}))

    def accept_batch(self, remote_addr: str, batch_id: str) -> None:
        self._schedule(self._send_raw("IPC_ACCEPT_BATCH", {
            "remote_addr": remote_addr, "batch_id": batch_id
        }))

    def reject_batch(self, remote_addr: str, batch_id: str) -> None:
        self._schedule(self._send_raw("IPC_REJECT_BATCH", {
            "remote_addr": remote_addr, "batch_id": batch_id
        }))

    def accept_pairing(self, remote_addr: str) -> None:
        self._schedule(self._send_raw("IPC_ACCEPT_PAIRING", {"remote_addr": remote_addr}))

    def reject_pairing(self, remote_addr: str) -> None:
        self._schedule(self._send_raw("IPC_REJECT_PAIRING", {"remote_addr": remote_addr}))

    def send_file(self, remote_addr: str, file_path: str) -> None:
        self._schedule(self._send_raw("IPC_SEND_FILE", {
            "remote_addr": remote_addr, "file_path": file_path
        }))

    def send_batch(self, remote_addr: str, paths: list[str], batch_name: str = "Batch") -> None:
        self._schedule(self._send_raw("IPC_SEND_BATCH", {
            "remote_addr": remote_addr, "paths": paths, "batch_name": batch_name
        }))

    def connect_peer(self, device_id: str) -> None:
        self._schedule(self._send_raw("IPC_CONNECT_PEER", {"device_id": device_id}))

    def get_history(self, limit: int = 50) -> None:
        self._schedule(self._send_raw("IPC_GET_HISTORY", {"limit": limit}))

    def get_trusted_devices(self) -> None:
        self._schedule(self._send_raw("IPC_GET_TRUSTED_DEVICES", {}))

    def remove_trusted_device(self, public_key_b64: str) -> None:
        self._schedule(self._send_raw("IPC_REMOVE_TRUSTED_DEVICE", {"public_key_b64": public_key_b64}))

    def set_clipboard_sync(self, enabled: bool) -> None:
        self._schedule(self._send_raw("IPC_SET_CLIPBOARD_SYNC", {"enabled": enabled}))

    def send_clipboard_text(self, text: str) -> None:
        self._schedule(self._send_raw("IPC_CLIPBOARD_SEND", {"text": text}))

    def discard_resume(self, transfer_id: str) -> None:
        self._schedule(self._send_raw("IPC_DISCARD_RESUME", {"transfer_id": transfer_id}))

    def request_resume(self, remote_addr: str, transfer_id: str) -> None:
        self._schedule(self._send_raw("IPC_REQUEST_RESUME", {
            "remote_addr": remote_addr, "transfer_id": transfer_id
        }))

    def send_clipboard(self, text: str) -> None:
        self._schedule(self._send_raw("IPC_CLIPBOARD_SEND", {"text": text}))

    @property
    def is_connected(self) -> bool:
        return self._connected


# ------------------------------------------------------------------
# Shared frame helpers (duplicated here to keep module self-contained)
# ------------------------------------------------------------------

def _encode_ipc_frame(msg_type: str, payload: dict) -> bytes:
    envelope = {
        "type": msg_type,
        "ts": int(time.time() * 1000),
        "payload": payload,
    }
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return IPC_MAGIC + struct.pack("!I", len(body)) + body


async def _read_ipc_frame(reader: asyncio.StreamReader) -> Optional[dict]:
    try:
        header = await reader.readexactly(6)
    except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
        return None
    if header[:2] != IPC_MAGIC:
        return None
    (length,) = struct.unpack("!I", header[2:])
    if length > IPC_MAX_FRAME:
        return None
    try:
        body = await reader.readexactly(length)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None
