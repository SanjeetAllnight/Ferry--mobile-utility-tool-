"""
Ferry Notification Bridge — Linux side.

Forwards mirrored Android notifications to the desktop via
org.freedesktop.Notifications (D-Bus FreeDesktop notification spec).

Design constraints (per spec):
  - Display-only: no actions, no icons from Android.
  - Server-issued UINT32 replaces_id — never invent one ourselves.
  - Bounded LRU map: max 200 ferry_id → dbus_id entries.
  - Rate limit: token bucket (10 burst, 1/sec sustained).
  - Markup escaping applied before calling Notify().
  - Notification title/body MUST NOT appear in logs.
  - Malformed payloads are dropped; the authenticated session is preserved.
"""

from __future__ import annotations

import html
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger("ferry.notification_bridge")

_DBUS_SERVICE   = "org.freedesktop.Notifications"
_DBUS_OBJECT    = "/org/freedesktop/Notifications"
_DBUS_IFACE     = "org.freedesktop.Notifications"
_FERRY_APP_NAME = "Raven"
_NOTIFY_TIMEOUT = 5000

_LRU_MAX        = 200
_LRU_TTL_SECS   = 4 * 3600

_RL_BURST       = 10
_RL_RATE        = 1.0


@dataclass
class _IdEntry:
    dbus_id: int
    expires_at: float


class _TokenBucket:
    def __init__(self, burst: int, rate: float) -> None:
        self._burst  = burst
        self._rate   = rate
        self._tokens = float(burst)
        self._last   = time.monotonic()

    def consume(self) -> bool:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class NotificationBridge:
    """Routes mirrored Android notifications to org.freedesktop.Notifications."""

    def __init__(self) -> None:
        self._proxy          = None
        self._capabilities: List[str] = []
        self._id_map: OrderedDict[str, _IdEntry] = OrderedDict()
        self._rate           = _TokenBucket(_RL_BURST, _RL_RATE)
        self._available      = False

    def start(self) -> None:
        """Connect to the D-Bus Notifications service and cache capabilities."""
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio

            self._proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                _DBUS_SERVICE,
                _DBUS_OBJECT,
                _DBUS_IFACE,
                None,
            )
            result = self._proxy.call_sync(
                "GetCapabilities",
                None,
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            caps = result.unpack()[0] if result else []
            self._capabilities = list(caps)
            logger.info("NotificationBridge connected. Capabilities: %s", self._capabilities)
            self._proxy.connect("g-signal", self._on_dbus_signal)
            self._available = True
        except Exception as exc:
            logger.warning("NotificationBridge: could not connect to D-Bus: %s", exc)
            self._available = False

    def _on_dbus_signal(self, proxy, sender_name, signal_name, parameters) -> None:
        if signal_name == "NotificationClosed":
            try:
                dbus_id, _reason = parameters.unpack()
                self._remove_by_dbus_id(int(dbus_id))
            except Exception:
                pass

    def post(self, ferry_id: str, app_label: str, title: str, body: str) -> bool:
        """Display or update a notification. Never logs title or body."""
        if not self._available or self._proxy is None:
            return False
        if not self._rate.consume():
            logger.debug("NotificationBridge: rate-limited, dropping notification")
            return False

        safe_summary = _escape(app_label)
        safe_body    = _escape(title)
        if body:
            safe_body += "\n" + _escape(body)

        replaces_id = self._get_replaces_id(ferry_id)

        try:
            import gi
            from gi.repository import GLib, Gio
            result = self._proxy.call_sync(
                "Notify",
                GLib.Variant(
                    "(susssasa{sv}i)",
                    (
                        _FERRY_APP_NAME,
                        replaces_id,
                        "",
                        safe_summary,
                        safe_body,
                        [],
                        {},
                        _NOTIFY_TIMEOUT,
                    ),
                ),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            issued_id = int(result.unpack()[0])
            self._store_id(ferry_id, issued_id)
            self._expire_old()
            return True
        except Exception as exc:
            logger.debug("NotificationBridge: Notify() failed: %s", exc)
            return False

    def remove(self, ferry_id: str) -> None:
        """Close a notification by its ferry_id. Ignores unknown IDs."""
        if not self._available or self._proxy is None:
            return
        entry = self._id_map.get(ferry_id)
        if entry is None:
            return
        try:
            import gi
            from gi.repository import GLib, Gio
            self._proxy.call_sync(
                "CloseNotification",
                GLib.Variant("(u)", (entry.dbus_id,)),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
        except Exception as exc:
            logger.debug("NotificationBridge: CloseNotification(%d) failed: %s", entry.dbus_id, exc)
        self._id_map.pop(ferry_id, None)

    def _get_replaces_id(self, ferry_id: str) -> int:
        entry = self._id_map.get(ferry_id)
        return entry.dbus_id if entry else 0

    def _store_id(self, ferry_id: str, dbus_id: int) -> None:
        if ferry_id in self._id_map:
            self._id_map.move_to_end(ferry_id)
        self._id_map[ferry_id] = _IdEntry(
            dbus_id=dbus_id,
            expires_at=time.monotonic() + _LRU_TTL_SECS,
        )
        while len(self._id_map) > _LRU_MAX:
            self._id_map.popitem(last=False)

    def _remove_by_dbus_id(self, dbus_id: int) -> None:
        to_delete = [fid for fid, e in self._id_map.items() if e.dbus_id == dbus_id]
        for fid in to_delete:
            self._id_map.pop(fid, None)

    def _expire_old(self) -> None:
        now = time.monotonic()
        expired = [fid for fid, e in self._id_map.items() if e.expires_at < now]
        for fid in expired:
            self._id_map.pop(fid, None)


def _escape(text: str) -> str:
    """Escape pango markup-sensitive characters."""
    return html.escape(str(text), quote=False)
