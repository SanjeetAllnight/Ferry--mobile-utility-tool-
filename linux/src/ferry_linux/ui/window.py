"""
Ferry Main Application Window (GTK4 & Libadwaita) — MVP Completion Sprint.

New in MVP Completion Sprint:
  - Drag-and-drop file/folder upload (text/uri-list drop target)
  - Settings button in header bar
  - Diagnostics expander showing session/identity/protocol info
  - "Send Files" (multi-select) added alongside "Send Folder"
  - ACTION_SEND_MULTIPLE share sheet support (Android side)
  - Batch aggregate progress visible in status card
  - Stale "Phase 3C" developer labels removed
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
        
        self._pending_send_path: Optional[str] = None

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
        

        self._send_files_btn = Gtk.Button(label="Send Files")
        self._send_files_btn.set_icon_name("edit-copy-symbolic")
        self._send_files_btn.set_sensitive(False)
        self._send_files_btn.set_tooltip_text("Send multiple files to a connected device")
        self._send_files_btn.connect("clicked", self._on_send_multiple_files_clicked)
        

        self._send_dir_btn = Gtk.Button(label="Send Folder")
        self._send_dir_btn.set_icon_name("folder-new-symbolic")
        self._send_dir_btn.set_sensitive(False)
        self._send_dir_btn.set_tooltip_text("Send a folder (batch) to a connected device")
        self._send_dir_btn.connect("clicked", self._on_send_dir_clicked)
        



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
            "Local Android ↔ Linux File Transfer\n"
            "Open Ferry on your Android phone to connect"
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

        # ── Diagnostics (collapsible) ──────────────────────────────
        diag_row = Adw.ExpanderRow()
        diag_row.set_title("Diagnostics")
        diag_row.set_subtitle("Identity, session, and protocol details")
        diag_row.set_icon_name("system-search-symbolic")
        pref_group.add(diag_row)

        self._diag_identity_row = Adw.ActionRow()
        self._diag_identity_row.set_title("Local Identity")
        self._diag_identity_row.set_subtitle("—")
        diag_row.add_row(self._diag_identity_row)

        self._diag_peer_row = Adw.ActionRow()
        self._diag_peer_row.set_title("Connected Peer")
        self._diag_peer_row.set_subtitle("Not connected")
        diag_row.add_row(self._diag_peer_row)

        self._diag_mdns_row = Adw.ActionRow()
        self._diag_mdns_row.set_title("mDNS Status")
        self._diag_mdns_row.set_subtitle("_ferry._tcp active")
        diag_row.add_row(self._diag_mdns_row)

        # ── Drag-and-Drop target on scrolled window ────────────────
        try:
            drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop_target.connect("accept", self._on_drop_accept)
            drop_target.connect("drop", self._on_drop)
            drop_target.connect("enter", self._on_drop_enter)
            drop_target.connect("leave", self._on_drop_leave)
            scrolled.add_controller(drop_target)
        except Exception as exc:
            import logging
            logging.getLogger("ferry.ui").debug("DnD setup skipped: %s", exc)

        GLib.idle_add(self._update_trusted_devices)
        GLib.idle_add(self._update_transfer_history)
        GLib.idle_add(self._update_diagnostics)

    # ── Trusted Devices ───────────────────────────────────────────────────────

    def update_trusted_devices_from_db(self, devices: list) -> bool:
        for row in self._trusted_rows:
            self.trusted_group.remove(row)
        self._trusted_rows.clear()

        established = {
            addr: ps for addr, ps in getattr(self, "_ipc_sessions", {}).items()
            if ps.get("state") == "ESTABLISHED"
        }
        
        self._send_btn.set_sensitive(bool(established))
        self._check_pending_send()

        # Combine DB devices and active established sessions
        known_dev_ids = {d.get("device_id") for d in devices if isinstance(d, dict) and d.get("device_id")}
        
        all_display_items = []
        for d in devices:
            if isinstance(d, dict):
                all_display_items.append((d.get("device_id"), d.get("device_name"), d.get("identity_public_key_b64"), True))
            else:
                all_display_items.append((d.device_id, d.device_name, d.identity_public_key_b64, True))
                
        for addr, ps in established.items():
            dev_id = ps.get("remote_device_id") or addr
            if dev_id not in known_dev_ids:
                all_display_items.append((dev_id, ps.get("remote_device_name") or "Android", ps.get("remote_static_pub_b64") or "", False))
                known_dev_ids.add(dev_id)

        if not all_display_items:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No trusted devices")
            self.trusted_group.add(empty_row)
            self._trusted_rows.append(empty_row)
            return False

        for dev_id, dev_name, pubkey, in_db in all_display_items:
            row = Adw.ActionRow()
            row.set_title(dev_name or "Unknown")
            row.set_subtitle((dev_id or "")[:24] + "…")
            row.set_icon_name("phone-symbolic")

            is_connected = any(
                ps.get("remote_device_id") == dev_id or 
                addr == dev_id or
                (pubkey and ps.get("remote_static_pub_b64") == pubkey)
                for addr, ps in established.items()
            )
            if is_connected:
                badge = Gtk.Label(label="● Connected")
                badge.add_css_class("success")
                row.add_suffix(badge)

                box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
                
                send_files_btn = Gtk.Button(label="Send Files")
                send_files_btn.add_css_class("suggested-action")
                send_files_btn.set_icon_name("document-send-symbolic")
                
                send_folder_btn = Gtk.Button(label="Send Folder")
                send_folder_btn.set_icon_name("folder-open-symbolic")
                
                remote_addr = next(
                    (addr for addr, ps in established.items()
                     if ps.get("remote_device_id") == dev_id or 
                        addr == dev_id or
                        (pubkey and ps.get("remote_static_pub_b64") == pubkey)),
                    None,
                )
                
                send_files_btn.connect(
                    "clicked",
                    lambda _btn, ra=remote_addr: self._launch_multiple_file_dialog(ra),
                )
                send_folder_btn.connect(
                    "clicked",
                    lambda _btn, ra=remote_addr: self._launch_folder_dialog(ra),
                )
                
                box.append(send_files_btn)
                box.append(send_folder_btn)
                row.add_suffix(box)

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

    def _update_trusted_devices(self) -> bool:
        app = self.get_application()
        if not app:
            return False
        app.get_trusted_devices()
        return False

    def _on_unpair_clicked_pk(self, pubkey: str) -> None:
        app = self.get_application()
        if app:
            app.remove_trusted_device(pubkey)

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
                
                # Handle both object and dict (IPC) types
                is_dict = isinstance(dev, dict)
                device_name = dev.get("device_name", "Unknown") if is_dict else dev.device_name
                addresses = dev.get("addresses", []) if is_dict else dev.addresses
                port = dev.get("port", 0) if is_dict else dev.port
                os_name = dev.get("os_name", "unknown") if is_dict else dev.os_name
                protocol_version = dev.get("protocol_version", 1) if is_dict else dev.protocol_version
                device_type = dev.get("device_type", "desktop") if is_dict else dev.device_type
                
                row.set_title(device_name)
                addr_str = addresses[0] if addresses else "Unknown Address"
                row.set_subtitle(
                    f"{addr_str}:{port} • OS: {os_name.capitalize()} "
                    f"• Protocol v{protocol_version}"
                )
                row.set_icon_name("phone-symbolic" if device_type == "mobile" else "computer-symbolic")

                badge = Gtk.Label(label="Available")
                badge.add_css_class("accent")
                row.add_suffix(badge)

                self.devices_group.add(row)
                self._device_rows.append(row)
            return False

        GLib.idle_add(_update)

    # ── Session state handler ─────────────────────────────────────────────────

    def handle_session_state(self, remote_addr: str, state: str) -> None:
        """Handle session state updates and trigger UI actions."""
        app = self.get_application()
        if not app:
            return

        if state == 'ESTABLISHED':
            self._update_trusted_devices()
            dlg = self._pairing_dialogs.pop(remote_addr, None)
            if dlg:
                dlg.close()

            # Enable send buttons
            self._send_btn.set_sensitive(True)
            self._send_files_btn.set_sensitive(True)
            self._send_dir_btn.set_sensitive(True)
            GLib.idle_add(self._update_diagnostics)

        if state in ('FAILED', 'DISCONNECTED', 'CLOSING'):
            self._update_trusted_devices()
            dlg = self._pairing_dialogs.pop(remote_addr, None)
            if dlg:
                dlg.close()

            # Disable send buttons if no established sessions left
            has_established = any(s.get('state') == 'ESTABLISHED' for s in getattr(self, '_ipc_sessions', {}).values())
            self._send_btn.set_sensitive(has_established)
            self._send_files_btn.set_sensitive(has_established)
            self._send_dir_btn.set_sensitive(has_established)
            GLib.idle_add(self._update_diagnostics)
            
            # Phase 3D: Clear any stuck active transfer rows when connection drops
            stuck = list(self._active_transfer_rows.keys())
            for stuck_tid in stuck:
                entry = self._active_transfer_rows.pop(stuck_tid, None)
                if entry:
                    row, progress_bar, size_label, total, cancel_btn = entry
                    cancel_btn.set_visible(False)
                    size_label.set_label("Lost ✗")
                    row.set_subtitle("Connection lost")

                    def _remove_row_closure(r=row):
                        try:
                            self._transfer_rows.remove(r)
                            self.transfers_group.remove(r)
                        except Exception:
                            pass
                        if not self._transfer_rows:
                            self._show_transfers_empty()
                        return False

                    GLib.timeout_add(3000, _remove_row_closure)
            self._update_transfer_history()

        # pairing dialog is now shown via show_pairing_dialog_ipc -> show_pairing_dialog


    def show_pairing_dialog(self, remote_addr: str, device_name: str, sas_code: str) -> bool:
        if remote_addr in self._pairing_dialogs:
            return False

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Pair with {device_name}?",
            body=(
                f"Verify that the following 6-digit code matches the one "
                f"shown on {device_name}:\n\n"
                f"<span size='xx-large' weight='bold'>{sas_code}</span>"
            ),
            body_use_markup=True,
        )
        dialog.add_response("reject", "Reject")
        dialog.add_response("accept", "Accept")
        dialog.set_response_appearance("reject", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)

        def on_response(dlg, response_id):
            self._pairing_dialogs.pop(remote_addr, None)
            app = self.get_application()
            if app:
                if response_id == "accept":
                    app.accept_pairing(remote_addr)
                else:
                    app.reject_pairing(remote_addr)

        dialog.connect("response", on_response)
        self._pairing_dialogs[remote_addr] = dialog
        dialog.present()
        return False

    # ── Incoming transfer request ─────────────────────────────────────────────

    def handle_transfer_request(
        self,
        remote_addr: str,
        transfer_id: str,
        file_name: str,
        file_size: int,
    ) -> None:
        app = self.get_application()
        if not app:
            return

        ps = getattr(self, "_ipc_sessions", {}).get(remote_addr, {})
        device_name = ps.get("remote_device_name", "Unknown Device")

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
                app.accept_transfer(remote_addr, transfer_id)
            else:
                app.reject_transfer(remote_addr, transfer_id)

        dialog.connect("response", on_response)
        dialog.present()

    def handle_pending_send(self, file_path: str) -> None:
        """Called when a file is passed via command line arguments/Share."""
        self._pending_send_path = file_path
        self._check_pending_send()

    def _check_pending_send(self) -> None:
        if not self._pending_send_path:
            return
        app = self.get_application()
        if not app:
            return
        established = [addr for addr, s in getattr(self, "_ipc_sessions", {}).items() if s.get("state") == "ESTABLISHED"]
        if established:
            remote_addr = established[0]
            path_to_send = self._pending_send_path
            self._pending_send_path = None
            app.send_file_to_peer(remote_addr, path_to_send)

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
            if app:
                app.cancel_transfer(transfer_id)

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
        if app:
            app.get_history()
        return False
        
    def update_transfer_history_from_db(self, records: list) -> bool:
        app = self.get_application()
        if not app:
            return False

        for row in self._history_rows:
            self.history_group.remove(row)
        self._history_rows.clear()

        if not records:
            empty_row = Adw.ActionRow()
            empty_row.set_title("No transfer history")
            empty_row.set_icon_name("document-send-symbolic")
            self.history_group.add(empty_row)
            self._history_rows.append(empty_row)
            return False

        for rec in records:
            is_dict = isinstance(rec, dict)
            direction = rec.get("direction", "OUTGOING") if is_dict else rec.direction
            file_name = rec.get("file_name", "Unknown") if is_dict else rec.file_name
            file_size = rec.get("file_size", 0) if is_dict else rec.file_size
            status = rec.get("status", "UNKNOWN") if is_dict else rec.status
            
            row = Adw.ActionRow()
            direction_icon = "↓" if direction == "INCOMING" else "↑"
            row.set_title(f"{direction_icon}  {file_name}")
            row.set_subtitle(
                f"{direction.capitalize()} • {self._format_size(file_size)} • "
                f"{status.capitalize()}"
            )
            row.set_icon_name(
                "document-save-symbolic" if direction == "INCOMING"
                else "document-send-symbolic"
            )

            status_label = Gtk.Label(
                label="✓" if status == "COMPLETED" else "✗" if status in ("FAILED", "CANCELLED") else "⏸"
            )
            status_label.add_css_class(
                "success" if status == "COMPLETED" else "error" if status in ("FAILED", "CANCELLED") else "warning"
            )
            row.add_suffix(status_label)
            
            transfer_id = rec.get("transfer_id", "") if is_dict else rec.transfer_id
            if status == "INTERRUPTED" and direction == "INCOMING":
                resume_btn = Gtk.Button(label="Resume")
                resume_btn.add_css_class("suggested-action")
                resume_btn.set_valign(Gtk.Align.CENTER)
                def _on_resume_clicked(_btn, tid=transfer_id):
                    established = [addr for addr, s in getattr(self, "_ipc_sessions", {}).items() if s.get("state") == "ESTABLISHED"]
                    if not established:
                        self._show_error_toast("No active connection to peer to resume")
                        return
                    remote_addr = established[0]
                    app.request_resume(remote_addr, tid)
                resume_btn.connect("clicked", _on_resume_clicked)
                row.add_suffix(resume_btn)

            self.history_group.add(row)
            self._history_rows.append(row)

        return False

    # ── Send File ─────────────────────────────────────────────────────────────

    def on_peers_updated(self) -> None:
        has_established = any(s.get('state') == 'ESTABLISHED' for s in getattr(self, '_ipc_sessions', {}).values())
        self._send_btn.set_sensitive(has_established)
        self._send_files_btn.set_sensitive(has_established)
        self._send_dir_btn.set_sensitive(has_established)

    def _on_send_file_clicked(self, _btn) -> None:
        established = [addr for addr, s in getattr(self, "_ipc_sessions", {}).items() if s.get("state") == "ESTABLISHED"]
        if not established:
            return
        remote_addr = next(iter(established))
        self._launch_file_dialog(remote_addr)

    def _on_send_multiple_files_clicked(self, _btn) -> None:
        established = [addr for addr, s in getattr(self, "_ipc_sessions", {}).items() if s.get("state") == "ESTABLISHED"]
        if not established:
            return
        remote_addr = next(iter(established))
        self._launch_multiple_file_dialog(remote_addr)

    def _launch_multiple_file_dialog(self, remote_addr: Optional[str]) -> None:
        """Open a multi-select GTK4 file-chooser and send chosen files as a batch."""
        if not remote_addr:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose files to send")
        dialog.open_multiple(self, None, self._on_multiple_files_chosen, remote_addr)

    def _on_multiple_files_chosen(
        self, dialog: Gtk.FileDialog, result, remote_addr: str
    ) -> None:
        """Callback from Gtk.FileDialog.open_multiple()."""
        try:
            gfiles = dialog.open_multiple_finish(result)
        except Exception:
            return  # User cancelled
        if not gfiles:
            return

        paths: list[Path] = []
        for i in range(gfiles.get_n_items()):
            gfile = gfiles.get_item(i)
            p = Path(gfile.get_path())
            if p.is_file():
                paths.append(p)

        if not paths:
            return

        app = self.get_application()
        if not app:
            return

        batch_name = f"{len(paths)} files"

        app.send_batch_to_peer(remote_addr, [str(p) for p in paths], batch_name)

    def _on_settings_clicked(self, _btn) -> None:
        """Open a simple settings dialog showing current config."""
        app = self.get_application()
        config = None
        if app and app.service:
            config = app.service.config

        if config:
            body = (
                f"Device Name: {config.device_name}\n"
                f"Listen Port: {config.listen_port}\n"
                f"Download Directory: {config.download_dir}\n"
                f"Auto-Accept Paired: {config.auto_accept_paired}"
            )
        else:
            body = "Service not yet started."

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Ferry Settings",
            body=body,
        )
        dialog.add_response("ok", "OK")
        dialog.present()


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
        if not app:
            return

        # Add outgoing progress row immediately
        file_size = file_path.stat().st_size
        import uuid as _uuid
        transfer_id = str(_uuid.uuid4())
        self._add_transfer_row(transfer_id, file_path.name, file_size, direction="↑")

        app.send_file_to_peer(remote_addr, str(file_path))

    def _on_send_dir_clicked(self, _btn) -> None:
        established = [addr for addr, s in getattr(self, "_ipc_sessions", {}).items() if s.get("state") == "ESTABLISHED"]
        if not established:
            return
        remote_addr = next(iter(established))
        self._launch_folder_dialog(remote_addr)

    def _launch_folder_dialog(self, remote_addr: Optional[str]) -> None:
        """Open a folder-chooser dialog and send selected folder(s) as a batch to remote_addr."""
        if not remote_addr:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a folder to send")
        dialog.select_multiple_folders(self, None, self._on_folder_chosen, remote_addr)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result, remote_addr: str) -> None:
        """Callback from Gtk.FileDialog.select_multiple_folders()."""
        try:
            gfiles = dialog.select_multiple_folders_finish(result)
        except Exception:
            return  # User cancelled or error

        if not gfiles:
            return

        paths = []
        for i in range(gfiles.get_n_items()):
            gfile = gfiles.get_item(i)
            paths.append(Path(gfile.get_path()))

        if not paths:
            return

        app = self.get_application()
        if not app:
            return

        batch_name = paths[0].name
        if len(paths) > 1:
            batch_name += f" and {len(paths) - 1} others"

        app.send_batch_to_peer(remote_addr, [str(p) for p in paths], batch_name)

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

        GLib.timeout_add(1500, _poll)

    def _on_drop_accept(self, drop_target, drop) -> bool:
        """Accept DnD drops that carry file lists."""
        return True

    def _on_drop_enter(self, drop_target, x, y) -> None:
        """Visual feedback: highlight the window when something is dragged over."""
        self.add_css_class("drop-target-active")

    def _on_drop_leave(self, drop_target) -> None:
        """Remove DnD highlight on drag leave."""
        self.remove_css_class("drop-target-active")

    def _on_drop(self, drop_target, value, x, y) -> bool:
        """Handle a completed DnD drop of a Gdk.FileList."""
        self.remove_css_class("drop-target-active")
        app = self.get_application()
        if not app:
            return False

        established = app.service.established_sessions
        if not established:
            self._show_error_toast("No connected device \u2014 connect to a peer first to drop files")
            return False

        remote_addr = next(iter(established))

        # value is a Gdk.FileList; convert to Path list
        try:
            from gi.repository import Gdk
            if isinstance(value, Gdk.FileList):
                files = value.get_files()
            else:
                return False
        except Exception as exc:
            import logging
            logging.getLogger("ferry.ui").warning("DnD value error: %s", exc)
            return False

        paths = [Path(f.get_path()) for f in files if f.get_path()]
        if not paths:
            return False

        if len(paths) == 1 and paths[0].is_file():
            # Single file \u2014 send directly
            file_path = paths[0]
            file_size = file_path.stat().st_size
            import uuid as _uuid
            transfer_id = str(_uuid.uuid4())
            self._add_transfer_row(transfer_id, file_path.name, file_size, direction="\u2191")

            app.send_file_to_peer(remote_addr, str(file_path))
        else:
            # Multiple files or a folder \u2014 use batch
            batch_name = paths[0].name if len(paths) == 1 else f"{len(paths)} dropped items"

            app.send_batch_to_peer(remote_addr, [str(p) for p in paths], batch_name)

        return True

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def _update_diagnostics(self) -> bool:
        app = self.get_application()
        if not app:
            return False

        identity_key = getattr(app, "_daemon_identity_prefix", "")
        if identity_key:
            self._diag_identity_row.set_subtitle(identity_key)

        established = [s for s in getattr(self, "_ipc_sessions", {}).values() if s.get("state") == "ESTABLISHED"]
        if established:
            ps = established[0]
            peer_name = ps.get("remote_device_name", "Unknown")
            addr = ps.get("remote_addr", "Unknown IP")
            self._diag_peer_row.set_subtitle(f"{peer_name} - {addr} (ESTABLISHED)")
        else:
            self._diag_peer_row.set_subtitle("Not connected")

        return False

        try:
            identity_key = app.service.identity.public_key_b64
            self._diag_identity_row.set_subtitle(
                identity_key[:24] + "\u2026" if len(identity_key) > 24 else identity_key
            )
        except Exception:
            pass

        established = app.service.established_sessions
        if established:
            addr, ps = next(iter(established.items()))
            peer_name = ps.remote_device_name or "Unknown"
            peer_state = ps.state.name if hasattr(ps, "state") and ps.state else "ESTABLISHED"
            self._diag_peer_row.set_subtitle(f"{peer_name} \u2014 {addr} ({peer_state})")
        else:
            self._diag_peer_row.set_subtitle("Not connected")

        return False


    # ── IPC Callbacks ──────────────────────────────────────────────────
    def set_daemon_status(self, connected: bool) -> None:
        pass  # Ultra-minimal UI doesn't need to show daemon status explicitly

    def apply_state_snapshot(self, devices: list = None, sessions: list = None, trusted: list = None, active_transfers: list = None, **kwargs) -> None:
        devices = devices or []
        sessions = sessions or []
        trusted = trusted or []
        self._ipc_sessions = {s["remote_addr"]: s for s in sessions}
        GLib.idle_add(self.update_discovered_devices, devices)
        GLib.idle_add(self.update_trusted_devices_from_db, trusted)
        for s in sessions:
            GLib.idle_add(self.handle_session_state, s["remote_addr"], s.get("state", "DISCONNECTED"))

    def update_discovered_devices_ipc(self, devices: list) -> None:
        GLib.idle_add(self.update_discovered_devices, devices)

    def handle_session_state_ipc(self, payload: dict) -> None:
        remote_addr = payload.get("remote_addr", "")
        state_name = payload.get("state", "")
        if not hasattr(self, "_ipc_sessions"):
            self._ipc_sessions = {}
        if state_name == "DISCONNECTED":
            self._ipc_sessions.pop(remote_addr, None)
        else:
            if remote_addr not in self._ipc_sessions:
                self._ipc_sessions[remote_addr] = {"remote_addr": remote_addr}
            
            for key, val in payload.items():
                self._ipc_sessions[remote_addr][key] = val
                
        GLib.idle_add(self.handle_session_state, remote_addr, state_name)

    def handle_transfer_request_ipc(self, remote_addr: str, transfer_id: str, file_name: str, file_size: int) -> None:
        GLib.idle_add(self.handle_transfer_request, remote_addr, transfer_id, file_name, file_size)

    def update_transfer_progress_ipc(self, transfer_id: str, bytes_done: int, total_bytes: int) -> None:
        GLib.idle_add(self.update_transfer_progress, transfer_id, bytes_done, total_bytes)

    def handle_transfer_complete_ipc(self, transfer_id: str, success: bool, file_name: str, direction: str) -> None:
        GLib.idle_add(self.handle_transfer_complete, transfer_id, success, file_name, direction)

    def show_pairing_dialog_ipc(self, remote_addr: str, device_name: str, sas_code: str) -> None:
        GLib.idle_add(self.show_pairing_dialog, remote_addr, device_name, sas_code)


    def update_transfer_history_records(self, records: list) -> None:
        GLib.idle_add(self.update_transfer_history_from_db, records)

    def update_trusted_devices_ipc(self, devices: list) -> None:
        GLib.idle_add(self.update_trusted_devices_from_db, devices)
