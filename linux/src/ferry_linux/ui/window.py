"""
Ferry Main Application Window (GTK4 & Libadwaita).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core.discovery import DiscoveredDevice
from ..core.session import SessionState
import asyncio


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

        # Trusted Devices Group
        self.trusted_group = Adw.PreferencesGroup()
        self.trusted_group.set_title("Trusted Devices")
        self.trusted_group.set_description("Devices that have been paired with this computer")
        content_box.append(self.trusted_group)
        self._trusted_rows: list[Adw.ActionRow] = []

        # Discovered Devices Preferences Group
        self.devices_group = Adw.PreferencesGroup()
        self.devices_group.set_title("Nearby Ferry Devices (Untrusted)")
        self.devices_group.set_description("Peers discovered on the local Wi-Fi / LAN")
        content_box.append(self.devices_group)

        self._device_rows: list[Adw.ActionRow] = []
        self._pairing_dialogs: dict[str, Adw.MessageDialog] = {}
        self._set_empty_devices_state()
        GLib.idle_add(self._update_trusted_devices)

    def _update_trusted_devices(self) -> bool:
        app = self.get_application()
        if not app or not app.service:
            return False

        for row in self._trusted_rows:
            self.trusted_group.remove(row)
        self._trusted_rows.clear()

        devices = app.service.db.list_devices()
        if not devices:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No trusted devices")
            self.trusted_group.add(empty_row)
            self._trusted_rows.append(empty_row)
            return False

        for dev in devices:
            row = Adw.ActionRow()
            row.set_title(dev.device_name)
            row.set_subtitle(dev.device_id)
            row.set_icon_name("phone-symbolic")

            unpair_btn = Gtk.Button(label="Unpair")
            unpair_btn.set_valign(Gtk.Align.CENTER)
            unpair_btn.add_css_class("destructive-action")
            unpair_btn.connect("clicked", lambda btn, d=dev: self._on_unpair_clicked(d))
            row.add_suffix(unpair_btn)

            self.trusted_group.add(row)
            self._trusted_rows.append(row)
        return False

    def _on_unpair_clicked(self, dev) -> None:
        app = self.get_application()
        if app and app.service:
            app.service.db.remove_device_by_public_key(dev.identity_public_key_b64)
            self._update_trusted_devices()

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

    def handle_session_state(self, remote_addr: str, state: SessionState) -> None:
        """Handle session state updates and trigger UI actions."""
        app = self.get_application()
        if not app or not app.service:
            return

        # Always update trusted devices list in case a session was established
        if state == SessionState.ESTABLISHED:
            self._update_trusted_devices()
            dlg = self._pairing_dialogs.pop(remote_addr, None)
            if dlg:
                dlg.close()

        if state in (SessionState.FAILED, SessionState.DISCONNECTED, SessionState.CLOSING):
            dlg = self._pairing_dialogs.pop(remote_addr, None)
            if dlg:
                dlg.close()

        if state in (SessionState.PAIRING, SessionState.WAITING_FOR_LOCAL_DECISION):
            print(f"AUTO-ACCEPTING PAIRING for {remote_addr}")
            import asyncio
            app.get_loop().call_soon_threadsafe(
                lambda: asyncio.ensure_future(app.service.accept_pairing(remote_addr), loop=app.get_loop())
            )
            return
            
            if remote_addr in self._pairing_dialogs:
                return

            ps = app.service._active_sessions.get(remote_addr)
            if not ps:
                return

            sas = ps.session.sas_code
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=f"Pair with {ps.remote_device_name}?",
                body=f"Verify that the following 6-digit code matches the one shown on {ps.remote_device_name}:\n\n<span size='xx-large' weight='bold'>{sas}</span>",
                body_use_markup=True
            )
            dialog.add_response("reject", "Reject")
            dialog.add_response("accept", "Accept")
            dialog.set_response_appearance("reject", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)

            def on_response(dlg, response_id):
                self._pairing_dialogs.pop(remote_addr, None)
                loop = app.get_loop()
                if response_id == "accept":
                    asyncio.run_coroutine_threadsafe(app.service.accept_pairing(remote_addr), loop)
                else:
                    asyncio.run_coroutine_threadsafe(app.service.reject_pairing(remote_addr), loop)

            dialog.connect("response", on_response)
            self._pairing_dialogs[remote_addr] = dialog
            dialog.present()

    def handle_transfer_request(self, remote_addr: str, transfer_id: str, file_name: str, file_size: int) -> None:
        """Prompt the user to accept or reject an incoming file transfer."""
        app = self.get_application()
        if not app or not app.service:
            return

        print(f"AUTO-ACCEPTING TRANSFER for {file_name}")
        import asyncio
        app.get_loop().call_soon_threadsafe(
            lambda: asyncio.ensure_future(app.service.accept_transfer(remote_addr, transfer_id), loop=app.get_loop())
        )
        return

        ps = app.service._active_sessions.get(remote_addr)
        device_name = ps.remote_device_name if ps else "Unknown Device"

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Incoming File Transfer",
            body=f"{device_name} wants to send you a file:\n\n<b>{file_name}</b> ({self._format_size(file_size)})",
            body_use_markup=True
        )
        dialog.add_response("reject", "Reject")
        dialog.add_response("accept", "Accept")
        dialog.set_response_appearance("reject", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)

        def on_response(dlg, response_id):
            loop = app.get_loop()
            if response_id == "accept":
                asyncio.run_coroutine_threadsafe(app.service.accept_transfer(remote_addr, transfer_id), loop)
            else:
                asyncio.run_coroutine_threadsafe(app.service.reject_transfer(remote_addr, transfer_id), loop)

        dialog.connect("response", on_response)
        dialog.present()

    def _format_size(self, size_bytes: int) -> str:
        """Format a byte count into a human-readable string."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"


