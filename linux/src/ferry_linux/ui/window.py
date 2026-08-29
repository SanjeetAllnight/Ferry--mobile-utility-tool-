"""
Ferry Main Application Window (GTK4 & Libadwaita).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


class FerryMainWindow(Adw.ApplicationWindow):
    """Main desktop window for Ferry."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.set_title("Ferry")
        self.set_default_size(720, 560)

        # Main vertical container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(main_box)

        # Header Bar
        header_bar = Adw.HeaderBar()
        main_box.append(header_bar)

        # Content Box with Margins
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content_box.set_margin_top(16)
        content_box.set_margin_bottom(24)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        main_box.append(content_box)

        # Status Banner
        status_page = Adw.StatusPage()
        status_page.set_icon_name("network-wireless-symbolic")
        status_page.set_title("Ferry")
        status_page.set_description(
            "Local Android ↔ Arch Linux Integration\n"
            "Phase 1: Foundation & Architecture Active"
        )
        content_box.append(status_page)

        # Preferences / Status Group
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("System Status")
        pref_group.set_description("Local daemon and connectivity state")
        content_box.append(pref_group)

        # Service Status Row
        service_row = Adw.ActionRow()
        service_row.set_title("Ferry Core Service")
        service_row.set_subtitle("Local service engine initialized")
        service_row.set_icon_name("system-run-symbolic")
        
        status_label = Gtk.Label(label="Online")
        status_label.add_css_class("success")
        service_row.add_suffix(status_label)
        pref_group.add(service_row)

        # Protocol Row
        proto_row = Adw.ActionRow()
        proto_row.set_title("Wire Protocol")
        proto_row.set_subtitle("dev.ferry.v1 (Framed JSON / TLS 1.3)")
        proto_row.set_icon_name("dialog-password-symbolic")
        pref_group.add(proto_row)

        # Connected Devices Group Placeholder
        devices_group = Adw.PreferencesGroup()
        devices_group.set_title("Discovered & Paired Devices")
        devices_group.set_description("Device discovery and pairing will be active in Phase 2")
        content_box.append(devices_group)

        empty_row = Adw.ActionRow()
        empty_row.set_title("No Paired Devices")
        empty_row.set_subtitle("Start pairing from your Android device in Phase 2")
        empty_row.set_icon_name("phone-symbolic")
        devices_group.add(empty_row)
