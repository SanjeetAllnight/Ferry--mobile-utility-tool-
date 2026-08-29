"""
Ferry Main Application Window (GTK4 & Libadwaita).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core.discovery import DiscoveredDevice


class FerryMainWindow(Adw.ApplicationWindow):
    """Main desktop window for Ferry."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.set_title("Ferry")
        self.set_default_size(720, 580)

        # Main vertical container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(main_box)

        # Header Bar
        header_bar = Adw.HeaderBar()
        main_box.append(header_bar)

        # Scrolled container for responsive content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_box.append(scrolled)

        # Content Box with Margins
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content_box.set_margin_top(16)
        content_box.set_margin_bottom(24)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        scrolled.set_child(content_box)

        # Status Banner
        self.status_page = Adw.StatusPage()
        self.status_page.set_icon_name("network-wireless-symbolic")
        self.status_page.set_title("Ferry")
        self.status_page.set_description(
            "Local Android ↔ Arch Linux Integration\n"
            "Phase 2A: Local Discovery Active"
        )
        content_box.append(self.status_page)

        # Preferences / Status Group
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("System Status")
        pref_group.set_description("Local daemon and connectivity state")
        content_box.append(pref_group)

        # Service Status Row
        service_row = Adw.ActionRow()
        service_row.set_title("Ferry Core Discovery Service")
        service_row.set_subtitle("mDNS announcement and browser active (_ferry._tcp)")
        service_row.set_icon_name("system-run-symbolic")

        status_label = Gtk.Label(label="Browsing")
        status_label.add_css_class("success")
        service_row.add_suffix(status_label)
        pref_group.add(service_row)

        # Protocol Row
        proto_row = Adw.ActionRow()
        proto_row.set_title("Wire Protocol")
        proto_row.set_subtitle("dev.ferry.v1 (Framed JSON / mDNS)")
        proto_row.set_icon_name("dialog-password-symbolic")
        pref_group.add(proto_row)

        # Discovered Devices Preferences Group
        self.devices_group = Adw.PreferencesGroup()
        self.devices_group.set_title("Nearby Ferry Devices (Untrusted)")
        self.devices_group.set_description("Peers discovered on the local Wi-Fi / LAN")
        content_box.append(self.devices_group)

        self._device_rows: list[Adw.ActionRow] = []
        self._set_empty_devices_state()

    def _set_empty_devices_state(self) -> None:
        """Clear device rows and show empty placeholder."""
        for row in self._device_rows:
            self.devices_group.remove(row)
        self._device_rows.clear()

        empty_row = Adw.ActionRow()
        empty_row.set_title("No Ferry Devices Discovered")
        empty_row.set_subtitle("Ensure Ferry is open on your Android phone on the same Wi-Fi")
        empty_row.set_icon_name("network-wireless-offline-symbolic")
        self.devices_group.add(empty_row)
        self._device_rows.append(empty_row)

    def update_discovered_devices(self, devices: list[DiscoveredDevice]) -> None:
        """Dynamically update UI with discovered LAN peers (Thread-Safe)."""
        def _update() -> bool:
            for row in self._device_rows:
                self.devices_group.remove(row)
            self._device_rows.clear()

            if not devices:
                self._set_empty_devices_state()
                return False

            for dev in devices:
                row = Adw.ActionRow()
                row.set_title(dev.device_name)
                addr_str = dev.addresses[0] if dev.addresses else "Unknown Address"
                row.set_subtitle(f"{addr_str}:{dev.port} • OS: {dev.os_name.capitalize()} • Protocol v{dev.protocol_version}")

                if dev.device_type == "mobile":
                    row.set_icon_name("phone-symbolic")
                else:
                    row.set_icon_name("computer-symbolic")

                badge = Gtk.Label(label="Available")
                badge.add_css_class("accent")
                row.add_suffix(badge)

                self.devices_group.add(row)
                self._device_rows.append(row)
            return False

        GLib.idle_add(_update)
