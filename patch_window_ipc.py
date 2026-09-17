import re
from pathlib import Path

path = Path("linux/src/ferry_linux/ui/window.py")
content = path.read_text()

# 1. Restore System Status pref_group
content = content.replace(
    "# content_box.append(pref_group)  # Hidden for ultra-minimal polish",
    "content_box.append(pref_group)"
)

# 2. Fix _update_trusted_devices
content = re.sub(
    r"    def _update_trusted_devices\(self\) -> bool:.*?return False",
    """    def _update_trusted_devices(self) -> bool:
        app = self.get_application()
        if not app:
            return False
        app.get_trusted_devices()
        return False""",
    content,
    flags=re.DOTALL
)

# 3. Fix _on_unpair_clicked_pk
content = re.sub(
    r"    def _on_unpair_clicked_pk\(self, pubkey: str\) -> None:.*?self\._update_trusted_devices\(\)",
    """    def _on_unpair_clicked_pk(self, pubkey: str) -> None:
        app = self.get_application()
        if app:
            app.remove_trusted_device(pubkey)""",
    content,
    flags=re.DOTALL
)

# 4. Fix handle_session_state
content = re.sub(
    r"    def handle_session_state\(self, remote_addr: str, state: SessionState\) -> None:.*?if not app or not app\.service:.*?return",
    """    def handle_session_state(self, remote_addr: str, state: str) -> None:
        \"\"\"Handle session state updates and trigger UI actions.\"\"\"
        app = self.get_application()
        if not app:
            return""",
    content,
    flags=re.DOTALL
)

# Change SessionState enum references to string checks in handle_session_state
content = content.replace("state == SessionState.ESTABLISHED", "state == 'ESTABLISHED'")
content = content.replace("state in (SessionState.FAILED, SessionState.DISCONNECTED, SessionState.CLOSING)", "state in ('FAILED', 'DISCONNECTED', 'CLOSING')")
content = content.replace("state in (SessionState.PAIRING, SessionState.WAITING_FOR_LOCAL_DECISION)", "state in ('PAIRING', 'WAITING_FOR_LOCAL_DECISION')")

# Fix disabled send buttons check in handle_session_state
content = re.sub(
    r"            has_established = len\(app\.service\.established_sessions\) > 0",
    r"            has_established = any(s.get('state') == 'ESTABLISHED' for s in getattr(self, '_ipc_sessions', {}).values())",
    content
)

# Remove the app.service._active_sessions check in handle_session_state (it's handled in show_pairing_dialog)
content = re.sub(
    r"        if state in \('PAIRING', 'WAITING_FOR_LOCAL_DECISION'\):.*?dialog\.present\(\)",
    r"        # pairing dialog is now shown via show_pairing_dialog_ipc -> show_pairing_dialog",
    content,
    flags=re.DOTALL
)

# 5. Add show_pairing_dialog
show_pairing = """
    def show_pairing_dialog(self, remote_addr: str, device_name: str, sas_code: str) -> bool:
        if remote_addr in self._pairing_dialogs:
            return False

        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=f"Pair with {device_name}?",
            body=(
                f"Verify that the following 6-digit code matches the one "
                f"shown on {device_name}:\\n\\n"
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
"""
content = content.replace("    # ── Incoming transfer request ──", show_pairing + "\n    # ── Incoming transfer request ──")


# 6. Fix handle_transfer_request
content = re.sub(
    r"    def handle_transfer_request\(.*?if not app or not app\.service:.*?return.*?ps = app\.service\._active_sessions\.get\(remote_addr\).*?device_name = ps\.remote_device_name if ps else \"Unknown Device\"",
    """    def handle_transfer_request(
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
        device_name = ps.get("remote_device_name", "Unknown Device")""",
    content,
    flags=re.DOTALL
)
content = re.sub(
    r"                asyncio\.run_coroutine_threadsafe\(\n                    app\.service\.accept_transfer.*?loop\n                \)",
    "                app.accept_transfer(remote_addr, transfer_id)",
    content,
    flags=re.DOTALL
)
content = re.sub(
    r"                asyncio\.run_coroutine_threadsafe\(\n                    app\.service\.reject_transfer.*?loop\n                \)",
    "                app.reject_transfer(remote_addr, transfer_id)",
    content,
    flags=re.DOTALL
)


# 7. Fix _check_pending_send
content = re.sub(
    r"    def _check_pending_send\(self\) -> None:.*?app\.get_loop\(\)\n            \)",
    """    def _check_pending_send(self) -> None:
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
            app.send_file_to_peer(remote_addr, path_to_send)""",
    content,
    flags=re.DOTALL
)


# 8. Fix _on_cancel_clicked
content = re.sub(
    r"        def _on_cancel_clicked\(_btn\):.*?app\.get_loop\(\),\n                \)",
    """        def _on_cancel_clicked(_btn):
            cancel_btn.set_sensitive(False)
            app = self.get_application()
            if app:
                app.cancel_transfer(transfer_id)""",
    content,
    flags=re.DOTALL
)


# 9. Fix _update_transfer_history
content = re.sub(
    r"    def _update_transfer_history\(self\) -> bool:.*?records = app\.service\.db\.list_transfers\(limit=20\)",
    """    def _update_transfer_history(self) -> bool:
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
        self._history_rows.clear()""",
    content,
    flags=re.DOTALL
)


# 10. Fix _on_resume_clicked
content = re.sub(
    r"                def _on_resume_clicked\(_btn, tid=rec\.transfer_id\):.*?app\.get_loop\(\)\n                    \)",
    """                def _on_resume_clicked(_btn, tid=rec.transfer_id):
                    established = [addr for addr, s in getattr(self, "_ipc_sessions", {}).items() if s.get("state") == "ESTABLISHED"]
                    if not established:
                        self._show_error_toast("No active connection to peer to resume")
                        return
                    remote_addr = established[0]
                    app.request_resume(remote_addr, tid)""",
    content,
    flags=re.DOTALL
)


# 11. Fix on_peers_updated
content = re.sub(
    r"    def on_peers_updated\(self\) -> None:.*?self\._send_dir_btn\.set_sensitive\(has_established\)",
    """    def on_peers_updated(self) -> None:
        has_established = any(s.get('state') == 'ESTABLISHED' for s in getattr(self, '_ipc_sessions', {}).values())
        self._send_btn.set_sensitive(has_established)
        self._send_files_btn.set_sensitive(has_established)
        self._send_dir_btn.set_sensitive(has_established)""",
    content,
    flags=re.DOTALL
)

# 12. Fix _on_send_file_clicked, _on_send_multiple_files_clicked, _on_send_dir_clicked
for method in ["_on_send_file_clicked", "_on_send_multiple_files_clicked", "_on_send_dir_clicked"]:
    content = re.sub(
        rf"    def {method}\(self, _btn\) -> None:.*?established = app\.service\.established_sessions",
        rf"""    def {method}(self, _btn) -> None:
        established = [addr for addr, s in getattr(self, "_ipc_sessions", {{}}).items() if s.get("state") == "ESTABLISHED"]""",
        content,
        flags=re.DOTALL
    )

# 13. Fix _on_multiple_files_chosen, _on_file_chosen, _on_folder_chosen, _on_drop
content = re.sub(
    r"        async def _do_send_batch\(\):.*?asyncio\.run_coroutine_threadsafe\(_do_send_batch\(\), app\.get_loop\(\)\)",
    """        app.send_batch_to_peer(remote_addr, [str(p) for p in paths], batch_name)""",
    content,
    flags=re.DOTALL
)
content = re.sub(
    r"        async def _do_send\(\):.*?asyncio\.run_coroutine_threadsafe\(_do_send\(\), app\.get_loop\(\)\)",
    """        app.send_file_to_peer(remote_addr, str(file_path))""",
    content,
    flags=re.DOTALL
)
content = re.sub(
    r"            async def _send\(\):.*?asyncio\.run_coroutine_threadsafe\(_send\(\), app\.get_loop\(\)\)",
    """            app.send_file_to_peer(remote_addr, str(file_path))""",
    content,
    flags=re.DOTALL
)
content = re.sub(
    r"            async def _send_batch\(\):.*?asyncio\.run_coroutine_threadsafe\(_send_batch\(\), app\.get_loop\(\)\)",
    """            app.send_batch_to_peer(remote_addr, [str(p) for p in paths], batch_name)""",
    content,
    flags=re.DOTALL
)

# Strip app.service / app.get_loop checks inside those methods
content = content.replace("        if not app or not app.service or not app.get_loop():\n            return", "        if not app:\n            return")

# 14. Fix _update_diagnostics
content = re.sub(
    r"    def _update_diagnostics\(self\) -> bool:.*?return False",
    """    def _update_diagnostics(self) -> bool:
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

        return False""",
    content,
    flags=re.DOTALL
)

path.write_text(content)
print("Patched window.py")
