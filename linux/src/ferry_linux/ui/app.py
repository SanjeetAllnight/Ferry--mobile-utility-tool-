"""
Ferry Libadwaita Application.
"""

from __future__ import annotations

import sys
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from .window import FerryMainWindow


class FerryApplication(Adw.Application):
    """Main Adw.Application class for Ferry."""

    def __init__(self, app_id: str = "dev.ferry.Ferry") -> None:
        super().__init__(
            application_id=app_id,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self.window: FerryMainWindow | None = None
        self.service = None
        self._loop = None
        self._thread = None

    def do_activate(self) -> None:
        if not self.window:
            self.window = FerryMainWindow(application=self)
            self._start_service()
        self.window.present()

    def _start_service(self) -> None:
        from ..core.service import FerryService
        import threading
        import asyncio

        self.service = FerryService()
        self.service.discovery.add_listener(self._on_devices_changed)
        self.service.add_session_listener(self._on_session_changed)
        self.service.add_transfer_request_listener(self._on_transfer_request)
        self.service.add_transfer_progress_listener(self._on_transfer_progress)
        self.service.add_transfer_complete_listener(self._on_transfer_complete)

        def run_asyncio():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self.service.run_forever())

        self._thread = threading.Thread(target=run_asyncio, daemon=True)
        self._thread.start()

    def _on_devices_changed(self, devices: list) -> None:
        if self.service and self.window:
            GLib.idle_add(self.window.update_discovered_devices, devices)

    def _on_session_changed(self, remote_addr: str, state) -> None:
        if self.service and self.window:
            # We'll pass this to window so it can show the pairing dialog if needed
            GLib.idle_add(self.window.handle_session_state, remote_addr, state)

    def _on_transfer_request(self, remote_addr: str, transfer_id: str, file_name: str, file_size: int) -> None:
        if self.service and self.window:
            GLib.idle_add(self.window.handle_transfer_request, remote_addr, transfer_id, file_name, file_size)

    def _on_transfer_progress(self, transfer_id: str, bytes_done: int, total_bytes: int) -> None:
        if self.service and self.window:
            GLib.idle_add(self.window.update_transfer_progress, transfer_id, bytes_done, total_bytes)

    def _on_transfer_complete(self, transfer_id: str, success: bool, file_name: str, direction: str) -> None:
        if self.service and self.window:
            GLib.idle_add(self.window.handle_transfer_complete, transfer_id, success, file_name, direction)

    def get_service(self):
        return self.service

    def get_loop(self):
        return self._loop


def run_app(argv: list[str] | None = None) -> int:
    """Launch the GTK4/Libadwaita application."""
    if argv is None:
        argv = sys.argv
    app = FerryApplication()
    return app.run(argv)
