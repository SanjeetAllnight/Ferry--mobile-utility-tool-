"""
Ferry Libadwaita Application.

P1: The UI is now a pure IPC client that connects to the running Ferry daemon
(ferry --service) via $XDG_RUNTIME_DIR/ferry.sock.

If no daemon is running, the app auto-starts one as a subprocess and retries
the connection. This means:
  - Closing the Ferry window does NOT kill in-progress transfers.
  - The daemon can be running (via systemd or manually) before the UI opens.
  - On relaunch the UI reconnects and shows the current transfer state.

Legacy fallback: if FERRY_IN_PROCESS=1 is set, runs the old in-process mode
(useful for development / testing without systemd).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk  # noqa: E402

from .window import FerryMainWindow
from .ipc_client import FerryIPCClient

logger = logging.getLogger("ferry.app")


class FerryApplication(Adw.Application):
    """Main Adw.Application class for Ferry."""

    def __init__(self, app_id: str = "dev.ferry.Ferry") -> None:
        super().__init__(
            application_id=app_id,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.window: FerryMainWindow | None = None

        # P1: IPC client (primary mode — talks to daemon)
        self.ipc: FerryIPCClient = FerryIPCClient()

        # Legacy in-process service (only active when FERRY_IN_PROCESS=1 or --in-process)
        self.service = None
        self._loop = None
        self._thread = None
        self._in_process = os.environ.get("FERRY_IN_PROCESS", "0") == "1"

        # State received from daemon via IPC
        self._daemon_config: dict = {}
        self._daemon_identity_prefix: str = ""
        self._discovered_devices: list = []
        self._trusted_devices: list = []
        self._active_sessions: dict = {}  # remote_addr -> session info dict

    # ------------------------------------------------------------------
    # GTK application lifecycle
    # ------------------------------------------------------------------

    def do_activate(self) -> None:
        # Load custom CSS
        css_provider = Gtk.CssProvider()
        css_path = Path(__file__).parent / "style.css"
        if css_path.exists():
            css_provider.load_from_path(str(css_path))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        if not self.window:
            self.window = FerryMainWindow(application=self)
            self._start_backend()
        self.window.present()

        # Handle --send deferred callback
        pending_cb = getattr(self, "_pending_send_callback", None)
        if pending_cb:
            self._pending_send_callback = None
            pending_cb()

    def do_open(self, files: list, n_files: int, hint: str) -> None:
        """Handle files passed via CLI or D-Bus (Ferry Drop / --send)."""
        self.do_activate()
        if not files or not self.window:
            return
        paths = [f.get_path() for f in files if f.get_path()]
        if not paths:
            return
        GLib.idle_add(self._queue_send, paths)

    def _queue_send(self, paths: list[str]) -> bool:
        """Queue file(s) for sending once a session is established."""
        if not self.window:
            return False
        if len(paths) == 1:
            self.window.handle_pending_send(paths[0])
        else:
            self.window.handle_pending_send_batch(paths)
        return False

    # ------------------------------------------------------------------
    # Backend startup (IPC or in-process)
    # ------------------------------------------------------------------

    def _start_backend(self) -> None:
        if self._in_process:
            logger.info("FERRY_IN_PROCESS=1 — starting service in-process (legacy mode)")
            self._start_in_process_service()
        else:
            self._start_ipc_client()

    def _start_ipc_client(self) -> None:
        """Start the IPC client; auto-launch daemon if not running."""
        # Register event callbacks
        self.ipc.on("connected",          self._on_ipc_connected)
        self.ipc.on("disconnected",       self._on_ipc_disconnected)
        self.ipc.on("state_snapshot",     self._on_state_snapshot)
        self.ipc.on("device_update",      self._on_device_update)
        self.ipc.on("session_update",     self._on_session_update)
        self.ipc.on("transfer_request",   self._on_transfer_request)
        self.ipc.on("transfer_progress",  self._on_transfer_progress)
        self.ipc.on("transfer_complete",  self._on_transfer_complete)
        self.ipc.on("pairing_request",    self._on_pairing_request)
        self.ipc.on("clipboard_incoming", self._on_clipboard_incoming)
        self.ipc.on("history_result",     self._on_history_result)
        self.ipc.on("trusted_devices",    self._on_trusted_devices_result)

        # Try to auto-launch daemon if socket doesn't exist
        sock_path = FerryIPCClient._socket_path()
        if not Path(sock_path).exists():
            logger.info("Daemon socket not found — auto-launching ferry --service")
            self._launch_daemon()

        self.ipc.start()

    def _launch_daemon(self) -> None:
        """Launch the Ferry daemon as a background subprocess."""
        python = sys.executable
        module = "ferry_linux"
        # Use the same PYTHONPATH as we were launched with
        env = os.environ.copy()
        try:
            proc = subprocess.Popen(
                [python, "-m", module, "--service"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach from current process group
            )
            logger.info("Launched daemon with PID %d", proc.pid)
        except Exception as exc:
            logger.error("Failed to launch daemon: %s", exc)

    # ------------------------------------------------------------------
    # IPC event handlers (called on GTK main thread via GLib.idle_add)
    # ------------------------------------------------------------------

    def _on_ipc_connected(self) -> None:
        logger.info("IPC: connected to daemon")
        if self.window:
            self.window.set_daemon_status(connected=True)

    def _on_ipc_disconnected(self) -> None:
        logger.info("IPC: disconnected from daemon")
        if self.window:
            self.window.set_daemon_status(connected=False)

    def _on_state_snapshot(self, payload: dict) -> None:
        """Full state dump received on subscribe."""
        self._daemon_config = payload.get("config", {})
        self._daemon_identity_prefix = payload.get("identity_key_prefix", "")
        devices = payload.get("devices", [])
        sessions = payload.get("sessions", [])
        active_transfers = payload.get("active_transfers", [])

        if self.window:
            self.window.apply_state_snapshot(
                devices=devices,
                sessions=sessions,
                active_transfers=active_transfers,
                config=self._daemon_config,
                identity_prefix=self._daemon_identity_prefix,
            )

    def _on_device_update(self, payload: dict) -> None:
        devices = payload.get("devices", [])
        self._discovered_devices = devices
        if self.window:
            self.window.update_discovered_devices_ipc(devices)

    def _on_session_update(self, payload: dict) -> None:
        remote_addr = payload.get("remote_addr", "")
        state_name = payload.get("state", "")
        if self.window:
            self.window.handle_session_state_ipc(remote_addr, state_name)

    def _on_transfer_request(self, payload: dict) -> None:
        remote_addr = payload.get("remote_addr", "")
        transfer_id = payload.get("transfer_id", "")
        file_name = payload.get("file_name", "")
        file_size = payload.get("file_size", 0)
        if self.window:
            self.window.handle_transfer_request_ipc(remote_addr, transfer_id, file_name, file_size)

    def _on_transfer_progress(self, payload: dict) -> None:
        transfer_id = payload.get("transfer_id", "")
        bytes_done = payload.get("bytes_done", 0)
        total_bytes = payload.get("total_bytes", 0)
        if self.window:
            self.window.update_transfer_progress(transfer_id, bytes_done, total_bytes)

    def _on_transfer_complete(self, payload: dict) -> None:
        transfer_id = payload.get("transfer_id", "")
        success = payload.get("success", False)
        file_name = payload.get("file_name", "")
        direction = payload.get("direction", "")
        if self.window:
            self.window.handle_transfer_complete(transfer_id, success, file_name, direction)

    def _on_pairing_request(self, payload: dict) -> None:
        remote_addr = payload.get("remote_addr", "")
        device_name = payload.get("device_name", "")
        sas_code = payload.get("sas_code", "")
        if self.window:
            self.window.show_pairing_dialog_ipc(remote_addr, device_name, sas_code)

    def _on_clipboard_incoming(self, payload: dict) -> None:
        text = payload.get("text", "")
        if self.window:
            self.window.handle_clipboard_incoming(text)

    def _on_history_result(self, payload: dict) -> None:
        records = payload.get("records", [])
        if self.window:
            self.window.update_transfer_history_records(records)

    def _on_trusted_devices_result(self, payload: dict) -> None:
        devices = payload.get("devices", [])
        self._trusted_devices = devices
        if self.window:
            self.window.update_trusted_devices_ipc(devices)

    # ------------------------------------------------------------------
    # Forwarding helpers — UI calls these; they go to IPC or in-process
    # ------------------------------------------------------------------

    def accept_transfer(self, remote_addr: str, transfer_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.accept_transfer(remote_addr, transfer_id), self._loop
            )
        else:
            self.ipc.accept_transfer(remote_addr, transfer_id)

    def reject_transfer(self, remote_addr: str, transfer_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.reject_transfer(remote_addr, transfer_id), self._loop
            )
        else:
            self.ipc.reject_transfer(remote_addr, transfer_id)

    def cancel_transfer(self, transfer_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.cancel_transfer(transfer_id), self._loop
            )
        else:
            self.ipc.cancel_transfer(transfer_id)

    def accept_batch(self, remote_addr: str, batch_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.accept_batch(remote_addr, batch_id), self._loop
            )
        else:
            self.ipc.accept_batch(remote_addr, batch_id)

    def reject_batch(self, remote_addr: str, batch_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.reject_batch(remote_addr, batch_id), self._loop
            )
        else:
            self.ipc.reject_batch(remote_addr, batch_id)

    def accept_pairing(self, remote_addr: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.accept_pairing(remote_addr), self._loop
            )
        else:
            self.ipc.accept_pairing(remote_addr)

    def reject_pairing(self, remote_addr: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.reject_pairing(remote_addr), self._loop
            )
        else:
            self.ipc.reject_pairing(remote_addr)

    def send_file_to_peer(self, remote_addr: str, file_path: str) -> None:
        if self._in_process and self.service:
            import asyncio
            from pathlib import Path as P
            asyncio.run_coroutine_threadsafe(
                self.service.send_file(remote_addr, P(file_path)), self._loop
            )
        else:
            self.ipc.send_file(remote_addr, file_path)

    def send_batch_to_peer(self, remote_addr: str, paths: list[str], batch_name: str = "Batch") -> None:
        if self._in_process and self.service:
            import asyncio
            from pathlib import Path as P
            asyncio.run_coroutine_threadsafe(
                self.service.send_batch(remote_addr, [P(p) for p in paths], batch_name), self._loop
            )
        else:
            self.ipc.send_batch(remote_addr, paths, batch_name)

    def connect_to_peer_by_id(self, device_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            target = next(
                (d for d in self.service.discovered_devices if d.device_id == device_id),
                None,
            )
            if target:
                asyncio.run_coroutine_threadsafe(
                    self.service.connect_to_peer(target), self._loop
                )
        else:
            self.ipc.connect_peer(device_id)

    def get_history(self) -> None:
        if self._in_process and self.service and self.window:
            try:
                records = self.service.db.get_recent_transfers(limit=50)
                GLib.idle_add(self.window.update_transfer_history_from_db, records)
            except Exception as exc:
                logger.warning("get_history error: %s", exc)
        else:
            self.ipc.get_history()

    def get_trusted_devices(self) -> None:
        if self._in_process and self.service and self.window:
            try:
                devices = self.service.db.get_trusted_devices()
                GLib.idle_add(self.window.update_trusted_devices_from_db, devices)
            except Exception as exc:
                logger.warning("get_trusted_devices error: %s", exc)
        else:
            self.ipc.get_trusted_devices()

    def remove_trusted_device(self, public_key_b64: str) -> None:
        if self._in_process and self.service and self.window:
            try:
                self.service.db.remove_device_by_public_key(public_key_b64)
                devices = self.service.db.get_trusted_devices()
                GLib.idle_add(self.window.update_trusted_devices_from_db, devices)
            except Exception as exc:
                logger.warning("remove_trusted_device error: %s", exc)
        else:
            self.ipc.remove_trusted_device(public_key_b64)

    def set_clipboard_sync(self, enabled: bool) -> None:
        if self._in_process and self.service:
            if hasattr(self.service, "clipboard_sync"):
                self.service.clipboard_sync.set_enabled(enabled)
        else:
            self.ipc.set_clipboard_sync(enabled)

    def send_clipboard_text(self, text: str) -> None:
        if self._in_process and self.service:
            import time
            import asyncio
            from ..protocol.models import MessageType
            for ps in self.service._active_sessions.values():
                from ..core.session import SessionState
                if ps.state == SessionState.ESTABLISHED:
                    asyncio.run_coroutine_threadsafe(
                        self.service.send_encrypted(
                            ps,
                            MessageType.CLIPBOARD_SYNC,
                            {"text": text, "ts": int(time.time() * 1000)}
                        ),
                        self._loop
                    )
        else:
            self.ipc.send_clipboard_text(text)

    def discard_resume(self, transfer_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.discard_interrupted_transfer(transfer_id), self._loop
            )
        else:
            self.ipc.discard_resume(transfer_id)

    def request_resume(self, remote_addr: str, transfer_id: str) -> None:
        if self._in_process and self.service:
            import asyncio
            asyncio.run_coroutine_threadsafe(
                self.service.request_resume(remote_addr, transfer_id), self._loop
            )
        else:
            self.ipc.request_resume(remote_addr, transfer_id)

    # ------------------------------------------------------------------
    # Legacy in-process service (FERRY_IN_PROCESS=1)
    # ------------------------------------------------------------------

    def _start_in_process_service(self) -> None:
        from ..core.service import FerryService
        import threading
        import asyncio

        self.service = FerryService()
        self.service.notifications._app = self
        self.service.discovery.add_listener(self._on_devices_changed_legacy)
        self.service.add_session_listener(self._on_session_changed_legacy)
        self.service.add_transfer_request_listener(self._on_transfer_request_legacy)
        self.service.add_transfer_progress_listener(self._on_transfer_progress_legacy)
        self.service.add_transfer_complete_listener(self._on_transfer_complete_legacy)

        def run_asyncio():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self.service.run_forever())

        self._thread = threading.Thread(target=run_asyncio, daemon=True)
        self._thread.start()

    def _on_devices_changed_legacy(self, devices: list) -> None:
        if self.window:
            GLib.idle_add(self.window.update_discovered_devices, devices)

    def _on_session_changed_legacy(self, remote_addr: str, state) -> None:
        if self.window:
            GLib.idle_add(self.window.handle_session_state, remote_addr, state)

    def _on_transfer_request_legacy(self, remote_addr: str, transfer_id: str, file_name: str, file_size: int) -> None:
        if self.window:
            GLib.idle_add(self.window.handle_transfer_request, remote_addr, transfer_id, file_name, file_size)

    def _on_transfer_progress_legacy(self, transfer_id: str, bytes_done: int, total_bytes: int) -> None:
        if self.window:
            GLib.idle_add(self.window.update_transfer_progress, transfer_id, bytes_done, total_bytes)

    def _on_transfer_complete_legacy(self, transfer_id: str, success: bool, file_name: str, direction: str) -> None:
        if self.window:
            GLib.idle_add(self.window.handle_transfer_complete, transfer_id, success, file_name, direction)

    # Legacy compat: window.py may call app.service directly; provide shims
    def get_service(self):
        return self.service  # None in IPC mode

    def get_loop(self):
        return self._loop  # None in IPC mode


def run_app(argv: list[str] | None = None) -> int:
    """Launch the GTK4/Libadwaita application."""
    if argv is None:
        argv = sys.argv
    app = FerryApplication()
    return app.run(argv)
