import re
from pathlib import Path

path = Path("linux/src/ferry_linux/ui/window.py")
content = path.read_text()

# 1. Fix update_discovered_devices
content = re.sub(
    r"            for dev in devices:.*?self\._device_rows\.append\(row\)",
    """            for dev in devices:
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
                self._device_rows.append(row)""",
    content,
    flags=re.DOTALL
)

# 2. Fix update_trusted_devices_from_db (Add two buttons)
content = re.sub(
    r"                send_btn = Gtk\.Button\(label=\"Send\"\).*?row\.add_suffix\(send_btn\)",
    """                box = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
                
                send_files_btn = Gtk.Button(label="Send Files")
                send_files_btn.add_css_class("suggested-action")
                send_files_btn.set_icon_name("document-send-symbolic")
                
                send_folder_btn = Gtk.Button(label="Send Folder")
                send_folder_btn.set_icon_name("folder-open-symbolic")
                
                remote_addr = next(
                    (addr for addr, ps in established.items()
                     if ps.get("remote_device_id") == dev_id or addr == dev_id),
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
                row.add_suffix(box)""",
    content,
    flags=re.DOTALL
)

# 3. Add _launch_folder_dialog
launch_folder_code = """
    def _launch_folder_dialog(self, remote_addr: str | None) -> None:
        if not remote_addr:
            return
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a folder to send")
        dialog.select_multiple_folders(self, None, self._on_folder_chosen, remote_addr)
"""
if "_launch_folder_dialog" not in content:
    content = content.replace(
        "    def _launch_multiple_file_dialog(self, remote_addr: Optional[str]) -> None:",
        launch_folder_code + "\n    def _launch_multiple_file_dialog(self, remote_addr: Optional[str]) -> None:"
    )

path.write_text(content)
print("Patched window.py again")
