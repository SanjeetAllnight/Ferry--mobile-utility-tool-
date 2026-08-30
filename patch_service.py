import re

with open("linux/src/ferry_linux/core/service.py", "r") as f:
    content = f.read()

# 1. Add listeners list in __init__
init_patch = """        self._session_listeners: list[Callable[[str, SessionState], None]] = []
        self._transfer_request_listeners = []"""
content = content.replace("        self._session_listeners: list[Callable[[str, SessionState], None]] = []", init_patch)

# 2. Add listener method
listener_method = """    def add_transfer_request_listener(self, cb):
        self._transfer_request_listeners.append(cb)

    def _notify_session_change"""
content = content.replace("    def _notify_session_change", listener_method)

# 3. Add accept/reject methods
accept_reject_methods = """
    async def accept_transfer(self, remote_addr: str, transfer_id: str) -> None:
        incoming = self._incoming_transfers.get(transfer_id)
        if not incoming: return
        ps = self._active_sessions.get(remote_addr)
        if not ps: return
        
        incoming.begin()
        await self.send_encrypted(ps, MessageType.TRANSFER_ACCEPT, {"transfer_id": transfer_id})

    async def reject_transfer(self, remote_addr: str, transfer_id: str) -> None:
        incoming = self._incoming_transfers.pop(transfer_id, None)
        if not incoming: return
        ps = self._active_sessions.get(remote_addr)
        if not ps: return
        
        await self.send_encrypted(ps, MessageType.TRANSFER_REJECT, {"transfer_id": transfer_id})

    def _signal_transfer_event"""
content = content.replace("    def _signal_transfer_event", accept_reject_methods)

# 4. Modify _on_transfer_request
old_request = """        # Reject if already handling a transfer (MVP single-transfer limit)
        if self._incoming_transfers:
            logger.warning("Rejecting TRANSFER_REQUEST from %s: busy", ps.remote_addr)
            await self.send_encrypted(
                ps, MessageType.TRANSFER_REJECT,
                {"transfer_id": meta.transfer_id, "reason": "BUSY"}
            )
            return

        staging_dir = Path(self.config.download_dir) / "staging"
        incoming = IncomingTransfer(meta=meta, staging_dir=staging_dir)
        self._incoming_transfers[meta.transfer_id] = incoming

        # Phase 3A: auto-accept
        incoming.begin()
        await self.send_encrypted(
            ps, MessageType.TRANSFER_ACCEPT,
            {"transfer_id": meta.transfer_id}
        )
"""
new_request = """        # Reject if already handling a transfer (MVP single-transfer limit)
        if self._incoming_transfers:
            logger.warning("Rejecting TRANSFER_REQUEST from %s: busy", ps.remote_addr)
            await self.send_encrypted(
                ps, MessageType.TRANSFER_REJECT,
                {"transfer_id": meta.transfer_id, "reason": "BUSY"}
            )
            return

        download_dir = Path(self.config.download_dir)
        incoming = IncomingTransfer(meta=meta, download_dir=download_dir)
        self._incoming_transfers[meta.transfer_id] = incoming

        logger.info(f"TRANSFER_REQUEST pending approval for {meta.file_name} from {ps.remote_addr}")
        
        for listener in self._transfer_request_listeners:
            listener(ps.remote_addr, meta.transfer_id, meta.file_name, meta.file_size)
"""
content = content.replace(old_request, new_request)

with open("linux/src/ferry_linux/core/service.py", "w") as f:
    f.write(content)

print("Patched!")
