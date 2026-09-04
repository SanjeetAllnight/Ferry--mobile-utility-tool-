"""
Ferry Main Application Window (GTK4 & Libadwaita) — Phase 3C.

New in Phase 3C:
  - "Send File" button in header bar (active when ≥1 ESTABLISHED session)
  - Per-device "Send File" button on trusted device rows
  - Active Transfers group with live progress bars (incoming and outgoing)
  - Transfer History panel (last 20 transfers from DB)
  - Incoming transfer progress bar replaces approval dialog during receipt
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from ..core.discovery import DiscoveredDevice
from ..core.session import SessionState


class FerryMainWindow(Adw.ApplicationWindow):
    """Main desktop window for Ferry."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.set_title("Ferry")
        self.set_default_size(760, 640)

        # Track active transfer UIs: transfer_id -> (row, progress_bar, label)
        self._active_transfer_rows: dict = {}

        # Main vertical container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(main_box)

        # ── Header Bar ────────────────────────────────────────────────────
        header_bar = Adw.HeaderBar()
        main_box.append(header_bar)

        self._send_btn = Gtk.Button(label="Send File")
        self._send_btn.set_icon_name("document-send-symbolic")
        self._send_btn.add_css_class("suggested-action")
        self._send_btn.set_sensitive(False)
        self._send_btn.set_tooltip_text("Send a file to a connected device")
        self._send_btn.connect("clicked", self._on_send_file_clicked)
        header_bar.pack_end(self._send_btn)

        # ── Scrolled container ────────────────────────────────────────────
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        main_box.append(scrolled)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content_box.set_margin_top(16)
        content_box.set_margin_bottom(24)
        content_box.set_margin_start(24)
        content_box.set_margin_end(24)
        scrolled.set_child(content_box)

        # ── Status Banner ─────────────────────────────────────────────────
        self.status_page = Adw.StatusPage()
        self.status_page.set_icon_name("network-wireless-symbolic")
        self.status_page.set_title("Ferry")
        self.status_page.set_description(
            "Local Android ↔ Arch Linux File Transfer\n"
            "Phase 3C: Full Transfer UI Active"
        )
        content_box.append(self.status_page)

        # ── System Status ─────────────────────────────────────────────────
        pref_group = Adw.PreferencesGroup()
        pref_group.set_title("System Status")
        pref_group.set_description("Local daemon and connectivity state")
        content_box.append(pref_group)

        service_row = Adw.ActionRow()
        service_row.set_title("Ferry Core Discovery Service")
        service_row.set_subtitle("mDNS announcement and browser active (_ferry._tcp)")
        service_row.set_icon_name("system-run-symbolic")
        status_label = Gtk.Label(label="Active")
        status_label.add_css_class("success")
        service_row.add_suffix(status_label)
        pref_group.add(service_row)

        proto_row = Adw.ActionRow()
        proto_row.set_title("Wire Protocol")
        proto_row.set_subtitle("dev.ferry.v1 (Framed JSON / ChaCha20-Poly1305)")
        proto_row.set_icon_name("dialog-password-symbolic")
        pref_group.add(proto_row)

        # ── Active Transfers ──────────────────────────────────────────────
        self.transfers_group = Adw.PreferencesGroup()
        self.transfers_group.set_title("Active Transfers")
        self.transfers_group.set_description("Transfers currently in progress")
        content_box.append(self.transfers_group)
        self._transfer_rows: list = []
        self._show_transfers_empty()

        # ── Trusted Devices ───────────────────────────────────────────────
        self.trusted_group = Adw.PreferencesGroup()
        self.trusted_group.set_title("Trusted Devices")
        self.trusted_group.set_description("Devices that have been paired with this computer")
        content_box.append(self.trusted_group)
        self._trusted_rows: list[Adw.ActionRow] = []

        # ── Nearby Devices ────────────────────────────────────────────────
        self.devices_group = Adw.PreferencesGroup()
        self.devices_group.set_title("Nearby Ferry Devices (Untrusted)")
        self.devices_group.set_description("Peers discovered on the local Wi-Fi / LAN")
        content_box.append(self.devices_group)
        self._device_rows: list[Adw.ActionRow] = []
        self._pairing_dialogs: dict[str, Adw.MessageDialog] = {}
        self._set_empty_devices_state()

        # ── Transfer History ──────────────────────────────────────────────
        self.history_group = Adw.PreferencesGroup()
        self.history_group.set_title("Transfer History")
        self.history_group.set_description("Recent completed file transfers")
        content_box.append(self.history_group)
        self._history_rows: list = []

        GLib.idle_add(self._update_trusted_devices)
        GLib.idle_add(self._update_transfer_history)

    # ── Trusted Devices ───────────────────────────────────────────────────────

    def _update_trusted_devices(self) -> bool:
        app = self.get_application()
        if not app or not app.service:
            return False

        for row in self._trusted_rows:
            self.trusted_group.remove(row)
        self._trusted_rows.clear()

        established = app.service.established_sessions
        devices = app.service.db.list_devices()

        self._send_btn.set_sensitive(bool(established))

        # Combine DB devices and active established sessions
        known_dev_ids = {d.device_id for d in devices}
        all_display_items = []
        for d in devices:
            all_display_items.append((d.device_id, d.device_name, d.identity_public_key_b64, True))
        for addr, ps in established.items():
            dev_id = ps.remote_device_id or addr
            if dev_id not in known_dev_ids:
                all_display_items.append((dev_id, ps.remote_device_name or "Android", ps.remote_static_pub_b64 or "", False))
                known_dev_ids.add(dev_id)

        if not all_display_items:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No trusted devices")
            self.trusted_group.add(empty_row)
            self._trusted_rows.append(empty_row)
            return False

        for dev_id, dev_name, pubkey, in_db in all_display_items:
            row = Adw.ActionRow()
            row.set_title(dev_name)
            row.set_subtitle(dev_id[:24] + "…")
            row.set_icon_name("phone-symbolic")

            # Connection badge
            is_connected = any(
                ps.remote_device_id == dev_id or addr == dev_id
                for addr, ps in established.items()
            )
            if is_connected:
                badge = Gtk.Label(label="● Connected")
                badge.add_css_class("success")
                row.add_suffix(badge)

                # Per-device Send File button
                send_btn = Gtk.Button(label="Send")
                send_btn.set_valign(Gtk.Align.CENTER)
                send_btn.add_css_class("suggested-action")
                send_btn.set_icon_name("document-send-symbolic")
                remote_addr = next(
                    (addr for addr, ps in established.items()
                     if ps.remote_device_id == dev_id or addr == dev_id),
                    None,
                )
                send_btn.connect(
                    "clicked",
                    lambda _btn, ra=remote_addr: self._launch_file_dialog(ra),
                )
                row.add_suffix(send_btn)

            if in_db:
                unpair_btn = Gtk.Button()
                unpair_btn.set_icon_name("user-trash-symbolic")
                unpair_btn.set_valign(Gtk.Align.CENTER)
                unpair_btn.add_css_class("destructive-action")
                unpair_btn.set_tooltip_text("Unpair")
                unpair_btn.connect("clicked", lambda _btn, pk=pubkey: self._on_unpair_clicked_pk(pk))
                row.add_suffix(unpair_btn)

            self.trusted_group.add(row)
            self._trusted_rows.append(row)
        return False

    def _on_unpair_clicked_pk(self, pubkey: str) -> None:
        app = self.get_application()
        if app and app.service:
            app.service.db.remove_device_by_public_key(pubkey)
            self._update_trusted_devices()

    # ── Nearby (untrusted) devices ────────────────────────────────────────────

    def _set_empty_devices_state(self) -> None:
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
        """Dynamically update UI with discovered LAN peers (Thread-Safe via GLib.idle_add)."""
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
                row.set_subtitle(
                    f"{addr_str}:{dev.port} • OS: {dev.os_name.capitalize()} "
                    f"• Protocol v{dev.protocol_version}"
                )
                row.set_icon_name("phone-symbolic" if dev.device_type == "mobile" else "computer-symbolic")

                badge = Gtk.Label(label="Available")
                badge.add_css_class("accent")
                row.add_suffix(badge)

                self.devices_group.add(row)
                self._device_rows.append(row)
            return False

        GLib.idle_add(_update)

    # ── Session state handler ─────────────────────────────────────────────────

    def handle_session_state(self, remote_addr: str, state: SessionState) -> None:
        """Handle session state updates and trigger UI actions."""
        app = self.get_application()
        if not app or not app.service:
            return

        if state == SessionState.ESTABLISHED:
            self._update_trusted_devices()
            dlg = self._pairing_dialogs.pop(remote_addr, None)
            if dlg:
                dlg.close()

        if state in (SessionState.FAILED, SessionState.DISCONNECTED, SessionState.CLOSING):
            self._update_trusted_devices()
            dlg = self._pairing_dialogs.pop(remote_addr, None)
            if dlg:
                dlg.close()

        if state in (SessionState.PAIRING, SessionState.WAITING_FOR_LOCAL_DECISION):
            if remote_addr in self._pairing_dialogs:
                return

            ps = app.service._active_sessions.get(remote_addr)
            if not ps:
                return

            sas = ps.session.sas_code
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=f"Pair with {ps.remote_device_name}?",
                body=(
                    f"Verify that the following 6-digit code matches the one "
                    f"shown on {ps.remote_device_name}:\n\n"
                    f"<span size='xx-large' weight='bold'>{sas}</span>"
                ),
                body_use_markup=True,
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

    # ── Incoming transfer request ─────────────────────────────────────────────

    def handle_transfer_request(
        self,
        remote_addr: str,
        transfer_id: str,
        file_name: str,
        file_size: int,
    ) -> None:
        """Prompt the user to accept or reject an incoming file transfer."""
        app = self.get_application()
        if not app or not app.service:
            return

        ps = app.service._active_sessions.get(remote_addr)
        device_name = ps.remote_device_name if ps else "Unknown Device"

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Incoming File Transfer",
            body=(
                f"{device_name} wants to send you a file:\n\n"
                f"<b>{GLib.markup_escape_text(file_name)}</b> "
                f"({self._format_size(file_size)})"
            ),
            body_use_markup=True,
        )
        dialog.add_response("reject", "Reject")
        dialog.add_response("accept", "Accept")
        dialog.set_response_appearance("reject", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)

        def on_response(dlg, response_id):
            loop = app.get_loop()
            if response_id == "accept":
                # Add an incoming progress row immediately
                self._add_transfer_row(transfer_id, file_name, file_size, direction="↓")
                asyncio.run_coroutine_threadsafe(
                    app.service.accept_transfer(remote_addr, transfer_id), loop
                )
            else:
                asyncio.run_coroutine_threadsafe(
                    app.service.reject_transfer(remote_addr, transfer_id), loop
                )

        dialog.connect("response", on_response)
        dialog.present()

    # ── Active transfer progress ──────────────────────────────────────────────

    def _show_transfers_empty(self) -> None:
        for row in self._transfer_rows:
            self.transfers_group.remove(row)
        self._transfer_rows.clear()

        empty_row = Adw.ActionRow()
        empty_row.set_title("No active transfers")
        empty_row.set_icon_name("document-send-symbolic")
        self.transfers_group.add(empty_row)
        self._transfer_rows.append(empty_row)

    def _add_transfer_row(
        self,
        transfer_id: str,
        file_name: str,
        total_bytes: int,
        direction: str = "↑",
    ) -> None:
        """Add a live progress row for an active transfer."""
        # Remove placeholder if present
        if len(self._transfer_rows) == 1:
            old = self._transfer_rows[0]
            if old.get_title() == "No active transfers":
                self.transfers_group.remove(old)
                self._transfer_rows.clear()

        row = Adw.ActionRow()
        row.set_title(f"{direction}  {file_name}")
        row.set_subtitle("Waiting…")
        row.set_icon_name(
            "document-save-symbolic" if direction == "↓" else "document-send-symbolic"
        )

        progress_bar = Gtk.ProgressBar()
        progress_bar.set_valign(Gtk.Align.CENTER)
        progress_bar.set_hexpand(True)
        progress_bar.set_fraction(0.0)
        row.add_suffix(progress_bar)

        size_label = Gtk.Label(label="0 B")
        size_label.add_css_class("dim-label")
        size_label.set_valign(Gtk.Align.CENTER)
        row.add_suffix(size_label)

        cancel_btn = Gtk.Button()
        cancel_btn.set_icon_name("process-stop-symbolic")
        cancel_btn.set_tooltip_text("Cancel transfer")
        cancel_btn.set_valign(Gtk.Align.CENTER)
        cancel_btn.add_css_class("flat")
        cancel_btn.add_css_class("destructive-action")

        def _on_cancel_clicked(_btn):
            cancel_btn.set_sensitive(False)
            app = self.get_application()
            if app and app.service and app.get_loop():
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    app.service.cancel_transfer(transfer_id),
                    app.get_loop(),
                )

        cancel_btn.connect("clicked", _on_cancel_clicked)
        row.add_suffix(cancel_btn)

        self.transfers_group.add(row)
        self._transfer_rows.append(row)
        self._active_transfer_rows[transfer_id] = (row, progress_bar, size_label, total_bytes, cancel_btn)

    def update_transfer_progress(
        self,
        transfer_id: str,
        bytes_done: int,
        total_bytes: int,
    ) -> bool:
        """Update the progress bar for an active transfer (called from GLib.idle_add)."""
        entry = self._active_transfer_rows.get(transfer_id)
        if entry is None:
            return False

        row, progress_bar, size_label, total, cancel_btn = entry
        fraction = (bytes_done / total) if total > 0 else (1.0 if bytes_done >= total else 0.0)
        progress_bar.set_fraction(min(fraction, 1.0))
        size_label.set_label(f"{self._format_size(bytes_done)} / {self._format_size(total)}")
        row.set_subtitle(f"{fraction * 100:.0f}%")
        return False

    def handle_transfer_complete(
        self,
        transfer_id: str,
        success: bool,
        file_name: str,
        direction: str,
    ) -> bool:
        """Called when a transfer finishes (success or failure)."""
        entry = self._active_transfer_rows.pop(transfer_id, None)
        if entry:
            row, progress_bar, size_label, total, cancel_btn = entry
            cancel_btn.set_visible(False)
            if success:
                progress_bar.set_fraction(1.0)
                size_label.set_label("Done ✓")
                row.set_subtitle("Completed")
            else:
                size_label.set_label("Failed ✗")
                row.set_subtitle("Transfer failed or cancelled")

            # Remove after a short delay
            def _remove_row():
                try:
                    self._transfer_rows.remove(row)
                    self.transfers_group.remove(row)
                except Exception:
                    pass
                if not self._transfer_rows:
                    self._show_transfers_empty()
                return False

            GLib.timeout_add(3000, _remove_row)

        # Refresh history panel
        self._update_transfer_history()
        return False

    # ── Transfer history panel ────────────────────────────────────────────────

    def _update_transfer_history(self) -> bool:
        app = self.get_application()
        if not app or not app.service:
            return False

        for row in self._history_rows:
            self.history_group.remove(row)
        self._history_rows.clear()

        records = app.service.db.list_transfers(limit=20)

        if not records:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No transfer history")
            empty_row.set_icon_name("document-send-symbolic")
            self.history_group.add(empty_row)
            self._history_rows.append(empty_row)
            return False

        for rec in records:
            row = Adw.ActionRow()
            direction_icon = "↓" if rec.direction == "INCOMING" else "↑"
            row.set_title(f"{direction_icon}  {rec.file_name}")
            row.set_subtitle(
                f"{rec.direction.capitalize()} • {self._format_size(rec.file_size)} • "
                f"{rec.status.capitalize()}"
            )
            row.set_icon_name(
                "document-save-symbolic" if rec.direction == "INCOMING"
                else "document-send-symbolic"
            )

            status_label = Gtk.Label(
                label="✓" if rec.status == "COMPLETED" else "✗"
            )
            status_label.add_css_class(
                "success" if rec.status == "COMPLETED" else "error"
            )
            row.add_suffix(status_label)

            self.history_group.add(row)
            self._history_rows.append(row)

        return False

    # ── Send File ─────────────────────────────────────────────────────────────

    def _on_send_file_clicked(self, _btn) -> None:
        """Send File header button: pick the first ESTABLISHED peer."""
        app = self.get_application()
        if not app or not app.service:
            return
        established = app.service.established_sessions
        if not established:
            return
        remote_addr = next(iter(established))
        self._launch_file_dialog(remote_addr)

    def _launch_file_dialog(self, remote_addr: Optional[str]) -> None:
        """Open a GTK4 file-chooser dialog and, on success, send the chosen file."""
        if not remote_addr:
            return

        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a file to send")
        dialog.open(self, None, self._on_file_chosen, remote_addr)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result, remote_addr: str) -> None:
        """Callback from Gtk.FileDialog.open()."""
        try:
            gfile = dialog.open_finish(result)
        except Exception:
            return  # User cancelled or error

        if gfile is None:
            return

        file_path = Path(gfile.get_path())
        if not file_path.is_file():
            self._show_error_toast(f"Not a file: {file_path.name}")
            return

        app = self.get_application()
        if not app or not app.service or not app.get_loop():
            return

        # Add outgoing progress row immediately
        file_size = file_path.stat().st_size
        import uuid as _uuid
        transfer_id = str(_uuid.uuid4())
        self._add_transfer_row(transfer_id, file_path.name, file_size, direction="↑")

        async def _do_send():
            try:
                success = await app.service.send_file(remote_addr, file_path, transfer_id=transfer_id)
                GLib.idle_add(
                    self.handle_transfer_complete,
                    transfer_id, success, file_path.name, "OUTGOING",
                )
            except Exception as exc:
                GLib.idle_add(
                    self.handle_transfer_complete,
                    transfer_id, False, file_path.name, "OUTGOING",
                )

        asyncio.run_coroutine_threadsafe(_do_send(), app.get_loop())

    # ── Toast helpers ─────────────────────────────────────────────────────────

    def _show_error_toast(self, message: str) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Error",
            body=message,
        )
        dialog.add_response("ok", "OK")
        dialog.present()

    # ── Utility ───────────────────────────────────────────────────────────────

    def _format_size(self, size_bytes: int) -> str:
        """Format a byte count into a human-readable string."""
        for unit in ["B", "KB", "MB", "GB"]:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"
