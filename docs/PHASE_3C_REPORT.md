# Phase 3C Implementation Report: Complete User-Facing File Transfer

**Status:** Completed & Physically Verified
**Scope:** Full user-facing file transfer UI on Linux (GTK4/Libadwaita) and Android (Jetpack Compose / Material 3), Storage Access Framework (SAF) integration, memory-efficient chunk streaming from `InputStream`, live active transfer progress bars, and bidirectional transfer history recording.

---

## 1. Overview & Architecture

Phase 3C completes the user-facing layer over the Phase 3A/3B secure multiplexed data transport.

```
┌────────────────────────────────────────────────────────┐
│                   Linux Desktop (GTK4)                 │
│  - "Send File" in HeaderBar + per-device Send buttons  │
│  - Gtk.FileDialog (async file picker)                  │
│  - Active Transfers group (Gtk.ProgressBar)            │
│  - Transfer History panel (SQLite backed)              │
└──────────────────────────┬─────────────────────────────┘
                           │  In-band FYCH Chunk Framing
                           │  over ChaCha20-Poly1305 TCP
┌──────────────────────────┴─────────────────────────────┐
│                  Android App (Compose M3)              │
│  - "Send File" Extended FAB                            │
│  - SAF ActivityResultContracts.GetContent("*/*")       │
│  - streamChunksFromStream (64 KiB chunks, no OOM)      │
│  - LinearProgressIndicator + TransferHistory card      │
└────────────────────────────────────────────────────────┘
```

---

## 2. Key Components Implemented

### Linux
- **`ferry_linux.ui.window.FerryMainWindow`**:
  - `Gtk.FileDialog` integration for picking files to send.
  - Active Transfers group with dynamic `Gtk.ProgressBar` rows updating per chunk.
  - Transfer History group listing recent transfers with direction icons (`↑` / `↓`), status badges (`✓` / `✗`), and formatted file sizes.
  - Incoming transfer approval dialog prompting with sender name, file name, and file size.
- **`ferry_linux.core.service.FerryService`**:
  - Added `established_sessions` property.
  - Added `add_transfer_progress_listener` and `add_transfer_complete_listener`.
  - Automatic persistence to `transfer_history` table in SQLite for both incoming and outgoing transfers.
- **`ferry_linux.ui.app.FerryApplication`**:
  - Event listeners wired through `GLib.idle_add` for thread-safe UI updates from asyncio daemon thread.

### Android
- **`dev.ferry.app.transfer.FerryTransferClient`**:
  - Added `streamChunksFromStream` to stream 64 KiB chunks directly from `InputStream` without loading the full file into memory.
  - Incremental SHA-256 computation via `MessageDigest`.
- **`dev.ferry.app.net.FerryControlClient`**:
  - Added `sendFile(uri: Uri, context: Context)` reading metadata and streaming via SAF ContentResolver.
  - `CompletableDeferred<Boolean>` for awaiting `TRANSFER_ACCEPT` without polling.
  - StateFlows for `transferProgress`, `incomingTransferProgress`, and `transferHistory`.
  - Proper receiver cleanup and cancellation in `finally` blocks.
- **`dev.ferry.app.ui.FerryApp`**:
  - Extended Floating Action Button ("Send File") visible when `ESTABLISHED`.
  - `ActivityResultContracts.GetContent()` launcher for system document picker.
  - `TransferProgressCard` rendering live progress and byte metrics.
  - `TransferHistoryCard` rendering recent transfers.
- **`dev.ferry.app.MainActivity`**:
  - Fixed Android lifecycle to avoid disconnect on `onStop()` when opening document pickers.
  - Added `ACTION_SEND` intent filter in `AndroidManifest.xml`.

---

## 3. Test Coverage

- **Linux Unit & Integration Tests**: 147 tests passing (0 regressions).
- **Android Unit Tests**: 50 tests passing (0 regressions).
- **Build Status**: Clean debug APK compilation and packaging.
