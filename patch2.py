with open("linux/src/ferry_linux/core/service.py", "r") as f:
    content = f.read()

import re
content = re.sub(
    r"staging_dir = Path\(self\.config\.download_dir\) / \"staging\".*?incoming = IncomingTransfer\(meta=meta, staging_dir=staging_dir\)",
    r"download_dir = Path(self.config.download_dir)\n        incoming = IncomingTransfer(meta=meta, download_dir=download_dir)",
    content,
    flags=re.DOTALL
)

content = re.sub(
    r"# Phase 3A: auto-accept.*?incoming\.begin\(\).*?await self\.send_encrypted\(.*?ps, MessageType\.TRANSFER_ACCEPT,.*?\{\"transfer_id\": meta\.transfer_id\}.*?\)",
    r"logger.info(f\"TRANSFER_REQUEST pending approval for {meta.file_name} from {ps.remote_addr}\")\n        for listener in self._transfer_request_listeners:\n            listener(ps.remote_addr, meta.transfer_id, meta.file_name, meta.file_size)",
    content,
    flags=re.DOTALL
)

with open("linux/src/ferry_linux/core/service.py", "w") as f:
    f.write(content)
