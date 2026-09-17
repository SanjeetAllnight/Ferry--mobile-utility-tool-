import re
from pathlib import Path

# Fix ipc.py DB model accesses
ipc_path = Path("linux/src/ferry_linux/core/ipc.py")
content = ipc_path.read_text()

# Fix GET_TRUSTED_DEVICES serialization
content = re.sub(
    r'                        "devices": \[\n\s*\{\n\s*"device_id": d\.device_id,\n\s*"device_name": d\.device_name,\n\s*"device_type": d\.device_type,\n\s*"public_key_b64": d\.public_key_b64,\n\s*"last_seen": d\.last_seen,\n\s*\}',
    """                        "devices": [
                            {
                                "device_id": getattr(d, "device_id", ""),
                                "device_name": getattr(d, "device_name", ""),
                                "identity_public_key_b64": getattr(d, "identity_public_key_b64", ""),
                                "last_seen": getattr(d, "last_seen", 0),
                            }""",
    content,
    flags=re.MULTILINE
)

# Fix REMOVE_TRUSTED_DEVICE serialization
content = re.sub(
    r'                        "devices": \[\n\s*\{\n\s*"device_id": d\.device_id,\n\s*"device_name": d\.device_name,\n\s*"device_type": d\.device_type,\n\s*"public_key_b64": d\.public_key_b64,\n\s*"last_seen": d\.last_seen,\n\s*\}',
    """                        "devices": [
                            {
                                "device_id": getattr(d, "device_id", ""),
                                "device_name": getattr(d, "device_name", ""),
                                "identity_public_key_b64": getattr(d, "identity_public_key_b64", ""),
                                "last_seen": getattr(d, "last_seen", 0),
                            }""",
    content,
    flags=re.MULTILINE
)
ipc_path.write_text(content)


# Fix window.py dict access
win_path = Path("linux/src/ferry_linux/ui/window.py")
content = win_path.read_text()

content = re.sub(
    r'        for rec in records:\n\s*row = Adw\.ActionRow\(\)\n\s*direction_icon = "↓" if rec\.direction == "INCOMING" else "↑"\n\s*row\.set_title\(f"\{direction_icon\}  \{rec\.file_name\}"\)\n\s*row\.set_subtitle\(\n\s*f"\{rec\.direction\.capitalize\(\)\} • \{self\._format_size\(rec\.file_size\)\} • "\n\s*f"\{rec\.status\.capitalize\(\)\}"\n\s*\)',
    """        for rec in records:
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
            )""",
    content,
    flags=re.MULTILINE
)
win_path.write_text(content)
print("Files patched.")
