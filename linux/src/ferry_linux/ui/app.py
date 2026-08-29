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

    def do_activate(self) -> None:
        if not self.window:
            self.window = FerryMainWindow(application=self)
        self.window.present()


def run_app(argv: list[str] | None = None) -> int:
    """Launch the GTK4/Libadwaita application."""
    if argv is None:
        argv = sys.argv
    app = FerryApplication()
    return app.run(argv)
