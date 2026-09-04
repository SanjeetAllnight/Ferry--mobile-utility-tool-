# Phase 3D Report — Transfer Reliability & Error Recovery

**Date**: 2026-09-04  
**Status**: ✅ COMPLETE  
**Automated Tests**: Linux 161/161 OK · Android 57/57 OK · APK build OK

---

## Objective

Harden the already-working file transfer system (Phase 3C) against real-world failures: network disconnects, disk-full conditions, source-file disappearance, and duplicate control messages — without implementing resumable transfers.

---

## Changes Made

### Linux — `transfer.py`

| Change | Severity Fixed |
|---|---|
| Added `TransferError` exception class for filesystem/IO errors | NEW |
| Added `TRANSFER_ACCEPT_TIMEOUT_SECS = 120.0` constant | NEW |
| `IncomingTransfer.begin()`: wrapped `mkdir`/`open` in try/except OSError; on failure transitions to FAILED and raises `TransferError` | HIGH |
| `IncomingTransfer.receive_chunk()`: wrapped `write` in try/except OSError; on failure calls `_cleanup_on_io_error()` and raises `TransferError` | HIGH |
| `IncomingTransfer.cancel()`: now safe to call from **any** state (IDLE, ACCEPTED, TRANSFERRING, COMPLETED); uses direct state assignment instead of `_transfer_transition` | HIGH |
| `IncomingTransfer._cleanup_on_io_error()`: new helper — closes file handle, removes `.part`, sets FAILED | NEW |
| `IncomingTransfer._cleanup_temp()`: improved logging for stale file removal | LOW |
| `OutgoingTransfer.stream_chunks()`: wrapped `open()` and `fh.read()` in try/except OSError; raises `TransferError` on failure | HIGH |
| State machine: added `IDLE→FAILED` and `IDLE→CANCELLED` as valid transitions | HIGH |

### Linux — `service.py`

| Change | Severity Fixed |
|---|---|
| Added `_record_transfer_once()` helper — writes history exactly once per `transfer_id`; second call is a no-op | MEDIUM |
| Added `_cancel_incoming_transfer_for_peer(ps)` — cancels all in-flight incoming transfers when session drops | HIGH |
| Added `_cancel_outgoing_transfer_for_peer(ps)` — cancels all outgoing transfers when session drops, unblocks waiting events | HIGH |
| `_run_session_loop` finally block: now calls both peer-cancel helpers before closing session | HIGH |
| Added `_accept_timeout_tasks` dict: tracks 120s acceptance timeout tasks per transfer_id | NEW |
| Added `_last_chunk_time` dict: tracks last chunk receipt time for stall detection | NEW |
| Added `_recorded_transfer_ids` set: prevents duplicate DB rows | MEDIUM |
| `_on_transfer_request()`: now starts a 120s acceptance timeout task; auto-rejects with `TIMEOUT_EXPIRED` if user does not respond | HIGH |
| `_on_transfer_complete()`: duplicate `TRANSFER_COMPLETE` is now silently ignored (idempotent) | MEDIUM |
| `_on_transfer_cancel()`: idempotent — if transfer already terminal, only unblocks events, no duplicate history | MEDIUM |
| `_on_chunk_received()`: now handles `TransferError` from disk write; sends `TRANSFER_ERROR` to sender, records FAILED | HIGH |
| `_on_chunk_received()`: records last chunk time for stall detection | NEW |
| `send_file()`: guards `build_metadata()` with try/except — source file disappearing before metadata is computed returns False | MEDIUM |
| `send_file()`: guards `stream_chunks()` async loop with `except TransferError` — sends TRANSFER_CANCEL to peer on IO error | HIGH |
| `send_file()`: all transfer history paths now go through `_record_transfer_once` (no more `_record_outgoing_transfer`) | MEDIUM |
| `cancel_transfer()`: cancels acceptance timeout if pending | MEDIUM |
| `accept_transfer()`: cancels acceptance timeout on user accept | MEDIUM |
| `reject_transfer()`: cancels acceptance timeout on user reject; calls `incoming.cancel()` | MEDIUM |
| `_cancel_incoming_transfer()`: now notifies UI via `_notify_transfer_complete` | MEDIUM |
| `start()`: scans `staging/` dir on startup and removes any orphaned `*.part` files from previous crashes | LOW |
| Imported `TransferError`, `TRANSFER_ACCEPT_TIMEOUT_SECS`, `TRANSFER_CHUNK_TIMEOUT_SECS` | NEW |

### Linux — `window.py`

| Change | Severity Fixed |
|---|---|
| `handle_session_state()`: on FAILED/DISCONNECTED/CLOSING, all active transfer rows are cleared with "Connection lost" label and removed after 3s delay | MEDIUM |
| `_update_transfer_history()` called after disconnect cleanup to refresh the history panel | LOW |

### Android — `FerryTransferReceiver.kt`

| Change | Severity Fixed |
|---|---|
| Added `failureCause: String?` property — set when IO failure occurs | NEW |
| `begin()`: wrapped `tempFile.createNewFile()` in try/catch IOException; on failure sets FAILED and rethrows | HIGH |
| `receiveChunk()`: wrapped `tempFile.appendBytes()` in try/catch IOException; on failure calls `cleanup()`, sets FAILED, sets `failureCause`, rethrows | HIGH |
| `cancel()`: doc clarified as safe from any state | LOW |

### Android — `FerryControlClient.kt`

| Change | Severity Fixed |
|---|---|
| Added `handledTransferIds: Set<String>` — deduplication set for terminal transfer control messages | MEDIUM |
| `runEncryptedSessionLoop` finally: checks for `activeReceiver != null` and writes a FAILED `TransferHistoryEntry` for interrupted incoming transfers | HIGH |
| `runEncryptedSessionLoop` finally: calls `pendingTransferAccept?.complete(false)` — unblocks `sendFile()` immediately on disconnect | HIGH |
| `runEncryptedSessionLoop` finally: clears `_transferProgress.value = null` | MEDIUM |
| `handleTransferMessage` — `TRANSFER_ACCEPT`, `TRANSFER_REJECT`, `TRANSFER_CANCEL`, `TRANSFER_COMPLETE`: all check `handledTransferIds` before processing; duplicate messages are no-ops | MEDIUM |
| `onTransferRequest()`: moved `activeReceiver = receiver` assignment AFTER `receiver.begin()`; guards `begin()` with try/catch IOException; sends `TRANSFER_ERROR` if begin fails | HIGH |
| `onTransferComplete()`: duplicate guard via `handledTransferIds` | MEDIUM |
| `appendHistory()`: now checks for duplicate `transferId` before appending; duplicate entries are silently dropped | MEDIUM |
| `sendFile()`: reset `outgoingCancelled`/`outgoingCancelledByPeer` flags at entry (before deferred allocation), not just before streaming | LOW |

---

## New Tests

### Linux — `test_transfer.py` (12 new Phase 3D reliability tests)

| Test | Verifies |
|---|---|
| `test_r01_begin_ioerror_sets_failed_state` | OSError during begin() → FAILED state |
| `test_r02_receive_chunk_diskfull_cleans_temp` | IOError during write → .part deleted, FAILED |
| `test_r03_cancel_from_idle_does_not_raise` | cancel() from IDLE is safe |
| `test_r04_cancel_from_accepted_does_not_raise` | cancel() from ACCEPTED is safe, .part deleted |
| `test_r05_stream_chunks_source_disappears` | FileNotFoundError before streaming → TransferError |
| `test_r06_cancel_from_completed_is_safe` | cancel() after finalise() does not raise |
| `test_r07_integrity_mismatch_no_file_published` | Hash mismatch → FAILED, no final file, no .part |
| `test_r08_transfer_error_is_exception` | TransferError inherits Exception |
| `test_r09_duplicate_history_protection` | _record_transfer_once is idempotent |
| `test_r10_cancel_incoming_for_peer_cleans_up` | Peer disconnect → .part deleted, FAILED history, UI notified |
| `test_r11_idle_to_failed_now_valid` | IDLE→FAILED is a valid Phase 3D transition |
| `test_r12_stale_part_cleanup_on_start` | Service start() removes orphaned .part files |

Also updated: `test_invalid_idle_to_cancelled` → `test_idle_to_cancelled_now_valid` (transition is now valid in Phase 3D)

**Total Linux tests: 161 (all passing)**

### Android — `TransferTest.kt` (5 new Phase 3D reliability tests)

| Test | Verifies |
|---|---|
| `test27_cancelFromIdleIsSafe` | cancel() from IDLE does not throw |
| `test28_receiveChunkAfterCancelIgnored` | receiveChunk after cancel throws IllegalStateException |
| `test29_integrityMismatchLeavesNoFile` | Hash mismatch → no .part, no final file |
| `test30_cancelFromCompletedIsSafe` | cancel() after finalise() does not throw |
| `test31_failureCauseSetOnHashMismatch` | After hash failure, .part file is deleted |

**Total Android tests: 57 (all passing)**

---

## Timeout Policy

| Timeout | Value | Scope |
|---|---|---|
| Handshake timeout | 30s | Session-level (existing) |
| Auth timeout | 30s | Session-level (existing) |
| Transfer accept response | 60s | Outgoing, wait_for (existing) |
| Transfer result wait | 60s | Outgoing, wait_for (existing) |
| **Acceptance timeout** | **120s** | **Incoming — NEW in Phase 3D** |
| Android accept deferred | 60s | withTimeout (existing) |
| Chunk stall constant | 120s | Defined in transfer.py (enforcement is via idle session timeout) |

---

## Cleanup Guarantee (All Failure Paths)

Every terminal state now ensures:
1. ✅ Open file handles are closed
2. ✅ `.part` files are deleted (unless finalise succeeded)
3. ✅ Entry removed from `_incoming_transfers` / `_active_outgoing_transfers`
4. ✅ Waiting asyncio events are unblocked
5. ✅ Transfer history recorded exactly once (idempotent)
6. ✅ `_notify_transfer_complete` called (UI row removed)
7. ✅ UI progress row cleared on session disconnect

---

## Physical Test Verification (Phase 3D.1)

The following tests were physically verified on real hardware (Realme Android 16 and Arch Linux) under manual conditions. See `docs/PHASE_3D.1_PHYSICAL_VERIFICATION.md` for full logs and file hashes.

| Test | Action | Result |
|---|---|---|
| **PT-1** | Send large file (>50 MiB), disconnect Android Wi-Fi mid-transfer | ✅ PASS. Transfer failed cleanly, `.part` file was deleted, and `FAILED` was recorded in the database history. Linux UI remained usable. |
| **PT-2** | Send large file, tap Cancel on Android | ✅ PASS. Cancellation message was processed mid-flight, `.part` was purged immediately, and `CANCELLED` was recorded in history. |
| **PT-3** | After PT-1 or PT-2, reconnect and send same file | ✅ PASS. Transfer succeeded with a new ID, final file size matched exactly, SHA-256 hash was verified, and `COMPLETED` was recorded in history without affecting prior failed entries. |

---

## Known Limitations

- **No chunk stall enforcement**: The `TRANSFER_CHUNK_TIMEOUT_SECS = 120.0` constant is defined but per-transfer stall detection (tracking `last_chunk_time`) only resets on each chunk. Active enforcement (a periodic asyncio task that checks `time.monotonic() - last_chunk_time`) is not yet implemented; the existing 300s session idle timeout remains the only backstop for truly stalled transfers.
- **Resumable transfers**: Not implemented per Phase 3D constraints; a failed transfer_id is terminal. Users restart via a new transfer.
- **Android chunk stall timeout**: Not implemented on Android; only session-level TCP timeout applies.
