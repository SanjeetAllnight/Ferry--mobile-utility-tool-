"""
Ferry Notification Manager — Linux side.

Sends desktop notifications using GLib.Notification (native GNOME/GTK4).
Falls back to a no-op if the notification API is unavailable.

Usage:
    notif = NotificationManager(app)   # pass the Gio.Application
    notif.incoming_transfer_request("Alice's Phone", "photo.jpg", 12345)
    notif.transfer_complete("photo.jpg", success=True)
    notif.transfer_failed("photo.jpg", "Connection lost")
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("ferry.notifications")

_GLib = None
_Gio = None
_GObject = None


def _try_import_gio():
    global _GLib, _Gio, _GObject
    if _Gio is None:
        try:
            import gi
            gi.require_version("GLib", "2.0")
            from gi.repository import GLib, Gio, GObject
            _GLib = GLib
            _Gio = Gio
            _GObject = GObject
        except Exception:
            pass


class NotificationManager:
    """
    Sends desktop notifications via GLib.Notification.

    All methods are safe to call from any thread (they schedule on the
    GLib main loop via GLib.idle_add).
    """

    def __init__(self, app=None) -> None:
        _try_import_gio()
        self._app = app  # Gio.Application / Gtk.Application

    def _send(self, title: str, body: str, icon: str = "ferry") -> None:
        if _GLib is None or self._app is None:
            logger.debug("Notification: [%s] %s", title, body)
            return

        def _do_send():
            try:
                notif = _Gio.Notification.new(title)
                notif.set_body(body)
                notif.set_priority(_Gio.NotificationPriority.NORMAL)
                # Use a generic system icon if available
                icon_obj = _Gio.ThemedIcon.new("dialog-information")
                notif.set_icon(icon_obj)
                self._app.send_notification(None, notif)
            except Exception as exc:
                logger.debug("Failed to send notification: %s", exc)

        _GLib.idle_add(_do_send)

    # ── Public API ─────────────────────────────────────────────────────────

    def incoming_transfer_request(
        self,
        device_name: str,
        file_name: str,
        file_size: int,
    ) -> None:
        """Notify user that an incoming file transfer request is waiting."""
        size_str = _format_size(file_size)
        self._send(
            "Incoming file from " + device_name,
            f"\"{file_name}\" ({size_str}) — open Ferry to accept or reject.",
        )

    def transfer_complete(self, file_name: str, success: bool, direction: str = "INCOMING") -> None:
        """Notify user that a transfer finished."""
        if success:
            verb = "received" if direction == "INCOMING" else "sent"
            self._send("Transfer complete", f"\"{file_name}\" {verb} successfully.")
        else:
            verb = "receive" if direction == "INCOMING" else "send"
            self._send("Transfer failed", f"Could not {verb} \"{file_name}\".")

    def transfer_cancelled(self, file_name: str) -> None:
        """Notify user that a transfer was cancelled."""
        self._send("Transfer cancelled", f"\"{file_name}\" transfer was cancelled.")

    def session_established(self, device_name: str) -> None:
        """Notify user that a secure session has been established."""
        self._send("Ferry connected", f"Secure session established with {device_name}.")

    def pairing_request(self, device_name: str, sas: str) -> None:
        """Notify user of incoming pairing request."""
        self._send(
            "Pairing request from " + device_name,
            f"Verify code: {sas} — open Ferry to accept or reject.",
        )

    def clipboard_sync_received(self, device_name: str) -> None:
        """Notify user that clipboard was updated from a remote device."""
        self._send("Clipboard updated", f"Clipboard synced from {device_name}.")


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024**2:.1f} MB"
    else:
        return f"{size_bytes / 1024**3:.2f} GB"
