with open("linux/src/ferry_linux/core/service.py", "r") as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if line.startswith("        self._session_listeners: list[Callable[[str, SessionState], None]] = []"):
        new_lines.append(line)
        new_lines.append("        self._transfer_request_listeners = []\n")
    elif line.startswith("    def _notify_session_change(self, remote_addr: str, state: SessionState) -> None:"):
        new_lines.append("    def add_transfer_request_listener(self, cb) -> None:\n")
        new_lines.append("        self._transfer_request_listeners.append(cb)\n\n")
        new_lines.append(line)
    elif line.startswith("    def _signal_transfer_event(self, ps: \"PeerSession\", attr: str, transfer_id: str, value) -> None:"):
        new_lines.append("    async def accept_transfer(self, remote_addr: str, transfer_id: str) -> None:\n")
        new_lines.append("        incoming = self._incoming_transfers.get(transfer_id)\n")
        new_lines.append("        if not incoming: return\n")
        new_lines.append("        ps = self._active_sessions.get(remote_addr)\n")
        new_lines.append("        if not ps: return\n")
        new_lines.append("        incoming.begin()\n")
        new_lines.append("        await self.send_encrypted(ps, MessageType.TRANSFER_ACCEPT, {\"transfer_id\": transfer_id})\n\n")
        new_lines.append("    async def reject_transfer(self, remote_addr: str, transfer_id: str) -> None:\n")
        new_lines.append("        incoming = self._incoming_transfers.pop(transfer_id, None)\n")
        new_lines.append("        if not incoming: return\n")
        new_lines.append("        ps = self._active_sessions.get(remote_addr)\n")
        new_lines.append("        if not ps: return\n")
        new_lines.append("        await self.send_encrypted(ps, MessageType.TRANSFER_REJECT, {\"transfer_id\": transfer_id})\n\n")
        new_lines.append(line)
    else:
        new_lines.append(line)

content = "".join(new_lines)
import re

content = re.sub(
    r"staging_dir = Path\(self\.config\.download_dir\) / \"staging\"\n        incoming = IncomingTransfer\(meta=meta, staging_dir=staging_dir\)",
    r"download_dir = Path(self.config.download_dir)\n        incoming = IncomingTransfer(meta=meta, download_dir=download_dir)",
    content
)

content = re.sub(
    r"        # Phase 3A: auto-accept.*?\{.*?\}",
    r"        logger.info(f\"TRANSFER_REQUEST pending approval for {meta.file_name} from {ps.remote_addr}\")\n        for listener in self._transfer_request_listeners:\n            listener(ps.remote_addr, meta.transfer_id, meta.file_name, meta.file_size)",
    content,
    flags=re.DOTALL
)

# wait there is a trailing `        )` after `{"transfer_id": meta.transfer_id}`
content = re.sub(
    r"        logger\.info\(f\"TRANSFER_REQUEST pending approval for \{meta\.file_name\} from \{ps\.remote_addr\}\"\)\n        for listener in self\._transfer_request_listeners:\n            listener\(ps\.remote_addr, meta\.transfer_id, meta\.file_name, meta\.file_size\)\n        \)\n",
    r"        logger.info(f\"TRANSFER_REQUEST pending approval for {meta.file_name} from {ps.remote_addr}\")\n        for listener in self._transfer_request_listeners:\n            listener(ps.remote_addr, meta.transfer_id, meta.file_name, meta.file_size)\n",
    content,
    flags=re.DOTALL
)

with open("linux/src/ferry_linux/core/service.py", "w") as f:
    f.write(content)
