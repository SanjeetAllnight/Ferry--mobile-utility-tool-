"""
Ferry Clipboard Sync — Linux side.

Synchronises plain-text clipboard content between a trusted Linux desktop
and a trusted Android device over the existing secure Ferry session.

Security model:
  - Only ESTABLISHED (trusted, authenticated) sessions may exchange clipboard
    frames. The service layer verifies session state before dispatching.
  - CLIPBOARD_SYNC payloads are encrypted inside the existing ChaCha20-Poly1305
    AEAD session; no plaintext clipboard data traverses the network.
  - An explicit user-controlled enable/disable toggle prevents passive leakage.
  - A loop-guard prevents echoing the same content back to the sender
    (i.e. the last text *we* received is not re-transmitted to the same peer).

Usage:
    mgr = ClipboardSyncManager(config)
    mgr.set_enabled(True)
    mgr.set_on_send_callback(async_fn)      # called when local clipboard changes

    # On incoming CLIPBOARD_SYNC from peer:
    mgr.on_remote_clipboard(text, peer_id)

    # On local clipboard change (UI layer calls this):
    await mgr.on_local_clipboard_changed(text, peer_id)
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Awaitable, Dict, Optional

logger = logging.getLogger("ferry.clipboard")

# Maximum clipboard text size to sync (512 KiB) — protects against inadvertent
# giant pastes consuming bandwidth.
MAX_CLIPBOARD_BYTES = 524288  # 512 KiB


class ClipboardSyncManager:
    """
    Manages bidirectional text clipboard synchronisation over a Ferry session.

    Thread-safety: all public methods must be called from the asyncio event loop
    thread (the same loop that runs FerryService).
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        # Last text we pushed *to* each peer (key: peer_id / remote_addr).
        # Used to deduplicate outgoing echoes.
        self._last_sent: Dict[str, str] = {}
        # Last text we received *from* each peer.
        # Used to avoid immediately echoing back what we just received.
        self._last_received: Dict[str, str] = {}
        # Callback: async fn(text, peer_id) -> None  — sends CLIPBOARD_SYNC to peer
        self._on_send: Optional[Callable[[str, str], Awaitable[None]]] = None
        # Callback: fn(text) -> None  — writes text to the local GTK clipboard
        self._on_write_local: Optional[Callable[[str], None]] = None
        # The current local clipboard text (updated by the UI layer)
        self._local_text: str = ""

    # ── Configuration ──────────────────────────────────────────────────────

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable clipboard synchronisation."""
        old = self._enabled
        self._enabled = enabled
        logger.info("Clipboard sync %s", "enabled" if enabled else "disabled")
        if not enabled:
            # Clear cached state so next enable starts fresh
            self._last_sent.clear()
            self._last_received.clear()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_on_send_callback(self, cb: Callable[[str, str], Awaitable[None]]) -> None:
        """Register the coroutine that sends CLIPBOARD_SYNC to a peer."""
        self._on_send = cb

    def set_on_write_local_callback(self, cb: Callable[[str], None]) -> None:
        """Register the function that writes text to the local clipboard."""
        self._on_write_local = cb

    # ── Outgoing (local → remote) ───────────────────────────────────────────

    async def on_local_clipboard_changed(self, text: str, peer_id: str) -> None:
        """
        Called by the UI layer when the local clipboard changes.

        Will push the text to *peer_id* unless:
          - clipboard sync is disabled
          - the text is identical to what was last received from this peer
            (loop-guard: avoids echoing back what we just wrote locally)
          - the text is identical to what was last sent to this peer
          - the text is empty
          - the text exceeds MAX_CLIPBOARD_BYTES
        """
        if not self._enabled:
            return
        if not text:
            return
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            logger.warning(
                "Local clipboard (%d bytes) exceeds max sync size — skipping",
                len(text.encode("utf-8")),
            )
            return
        # Loop-guard: if this is exactly what we last received from this peer,
        # skip (we just wrote it to the clipboard ourselves)
        if text == self._last_received.get(peer_id):
            return
        # Dedup: don't resend the same text twice
        if text == self._last_sent.get(peer_id):
            return

        self._last_sent[peer_id] = text
        self._local_text = text
        if self._on_send:
            try:
                await self._on_send(text, peer_id)
                logger.debug(
                    "Clipboard sync sent to %s (%d chars)", peer_id, len(text)
                )
            except Exception as exc:
                logger.error("Failed to send clipboard to %s: %s", peer_id, exc)

    # ── Incoming (remote → local) ───────────────────────────────────────────

    def on_remote_clipboard(self, text: str, peer_id: str) -> None:
        """
        Called by the service layer when a CLIPBOARD_SYNC frame arrives.

        Writes the text to the local clipboard unless:
          - clipboard sync is disabled
          - the text is empty or oversized
          - the text is identical to what we last received (dedup)
        """
        if not self._enabled:
            return
        if not text:
            return
        if len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
            logger.warning(
                "Remote clipboard (%d bytes) exceeds max sync size — discarding",
                len(text.encode("utf-8")),
            )
            return
        if text == self._last_received.get(peer_id):
            # Duplicate frame — already applied
            return

        self._last_received[peer_id] = text
        # Also record as sent so we don't immediately echo it back
        self._last_sent[peer_id] = text

        logger.debug(
            "Clipboard sync received from %s (%d chars) — writing locally",
            peer_id, len(text),
        )
        if self._on_write_local:
            try:
                self._on_write_local(text)
            except Exception as exc:
                logger.error("Failed to write remote clipboard locally: %s", exc)

    def clear_peer_state(self, peer_id: str) -> None:
        """Clear cached clipboard state for a disconnected peer."""
        self._last_sent.pop(peer_id, None)
        self._last_received.pop(peer_id, None)
