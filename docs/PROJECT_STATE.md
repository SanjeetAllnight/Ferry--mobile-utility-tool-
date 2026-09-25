# Ferry Project State & Persistent Context

This document is the primary persistent context file for **Ferry**. It reflects the factual, verified state of the codebase.

---

### Current Phase: MVP Completion Sprint (Complete)
**Status:** ✅ All sprint features implemented. 219/219 Linux tests passing (1 pre-existing port-reuse flaky). Android APK builds and unit tests pass. Physical device testing pending.
**Goal:** Close all remaining user-reported gaps: multi-file sharing, recursive folder enumeration, background transfers, batch progress, drag-and-drop, settings, Nautilus integration.

### Phase 4 Progress Tracking:
- **Phase 4A / 4B / 4C (Multi-File, Share Integration, Batch Transfers)**: Complete. Native Android Share sheet, Linux GTK batch folder send, Android `sendBatch()`. **Critical bug fixed:** Android `onBatchRequest()` was rejecting all incoming batches — now auto-accepts and tracks active batch ID so sequential TRANSFER_REQUESTs within a batch are accepted.
- **Phase 4D (Live State & UI Fixes)**: Complete. Fixed Android `NsdManager` stale cache presentation and Linux GTK `ESTABLISHED` state race condition (see `docs/LIVE_DISCOVERY_SESSION_FIX_REPORT.md`).
- **MVP Final Sprint**: Complete. Desktop notifications (GLib.Notification), systemd user service, `.desktop` entry, install script. See `docs/FINAL_MVP_IMPLEMENTATION_REPORT.md`.
- **MVP Completion Sprint**: Complete. See Section 3 below for full feature list.

### Previous Phase: Phase 3 (File Transfer Execution)
**Status:** Phase 3E Implementation Complete (Verification Partial)
**Goal:** Implement file transfer capabilities between paired devices.

### Phase 3 Progress Tracking:
- **Phase 3A (Transfer Architecture & Base Data Channel)**: Complete / Verified. TransferState and metadata tracking implemented on both OS platforms. In-band multiplexing via binary `FYCH` chunk frames established over existing ChaCha20-Poly1305 sessions (ADR 007). All unit tests passing (127 Linux, 50 Android). Physical Android ↔ Linux streaming and SHA-256 integrity verified over local Wi-Fi.
- **Phase 3B (Linux File Receiving & Transfer Approvals)**: Complete / Verified. `IncomingTransfer` refactored to parse directly to user's specified `download_dir`. Receiver `FerryService` extended to surface `TRANSFER_REQUEST` up to the UI. Fully integration tested on Linux and physically verified receiving a file from Android over Wi-Fi.
- **Phase 3C / 3C.1 (Sender & Receiver UI / File Picking & Physical QA)**: Complete / Fully Verified. Full bidirectional user-facing file transfer experience physically verified on real Android device and Arch Linux desktop. Linux `Gtk.FileDialog` and Android SAF `GetContent` picker validated. Bidirectional streaming (up to 16.5 MB at ~11.8 MB/s), SHA-256 integrity verification, live UI progress, incoming transfer approval, and transfer history panels verified.
- **Phase 3C.2 (Final Acceptance-Gate Verification & Discrepancy Resolution)**: Complete / Fully Verified. Resolved both remaining acceptance gate discrepancies: (1) native zero-byte (0-byte) file transfer support with SHA-256 `e3b0c442...` integrity verification, and (2) user-facing in-flight transfer cancellation UI buttons on GTK4 and Jetpack Compose with wire `TRANSFER_CANCEL` transmission, immediate `.part` cleanup, and `CANCELLED` history recording. 149/149 Linux unit tests and 52/52 Android unit tests passing (201 total). Phase 3C is officially CLOSED.
- **Phase 3D (Transfer Reliability & Error Recovery)**: Complete / Verified. Hardened the transfer system against all real-world failure modes: network disconnects mid-transfer, disk-full during writes, source-file disappearance during streaming, duplicate control messages, stale `.part` files from prior crashes, and acceptance timeouts. Session-disconnect cleanup now cancels in-flight transfers, records FAILED history, and clears UI rows. Added `TransferError` exception, `_record_transfer_once` idempotency guard, 120s acceptance timeout, and `_cancel_incoming_transfer_for_peer` / `_cancel_outgoing_transfer_for_peer` peer-cleanup helpers. 161/161 Linux tests and 57/57 Android tests passing (218 total). APK build successful. Phase 3D is CLOSED.
- **Phase 3D.1 (Physical Reliability Acceptance)**: All three physical tests (PT-1 disconnect, PT-2 cancel, PT-3 recovery) passed on real hardware. Phase 3D is **FULLY VERIFIED**.
- **Phase 3E (Resumable Transfer Architecture Design)**: Architecture design complete. See `docs/PHASE_3E_DESIGN.md`.
- **Phase 3E Task 1 (Persistent Interrupted Transfer Foundation)**: Complete. Linux DB schema migrated to version 3 (7 new resume columns). `INTERRUPTED` state added to `TransferState` with `TRANSFERRING→INTERRUPTED` transition. `IncomingTransfer.interrupt()` and `interrupt_info()` implemented. `InterruptedTransferInfo` dataclass and 4 new `DatabaseManager` methods added (`save_interrupted_transfer`, `get_interrupted_transfer`, `list_interrupted_transfers_for_peer`, `expire_interrupted_transfers`). 22 new unit tests. 183/183 Linux tests passing.
- **Phase 3E Task 2 (Resume Negotiation Protocol)**: Complete. Wire protocol models implemented for `TRANSFER_RESUME_REQUEST`, `TRANSFER_RESUME_ACCEPT`, `TRANSFER_RESUME_REJECT` with strict validation (UUID format, non-negative bounds, 64-char lowercase hex SHA-256, chunk alignment consistency, and explicit `ResumeRejectReason` enum). Implemented `IncomingTransfer.prepare_resume_request()` reading actual `.part` size from disk, verifying chunk boundaries, and streaming SHA-256 from disk without loading full file into memory. Added minimal service layer dispatch in `service.py`. 21 new unit tests (11 protocol model tests + 10 transfer tests).
- **Phase 3E Task 3 (Resume Execution & UI)**: Implementation complete. Wired up state machine transitions for `RESUME_REQUESTED` and `RESUMING`. Sender side prefix-hash computation and chunk stream skipping implemented. UI actions wired for Linux and Android. Android UI physical verification pending. Diagnostic and Clipboard UI elements intentionally removed to achieve minimal design goals. **Physical Verification Status:** Android-originated resume path (Android as Receiver) has been manually verified and **passed**. Linux-originated resume path (Linux as Receiver) currently has a known issue and is **not passing**. Existing non-resume transfers remain functional. Automated tests (219/219 Linux, all Android tests) continue to pass. The Linux resume issue is **deferred** to a later phase. **Phase 3E implementation is closed (Partial Verification).**

### Previous Phase: Phase 2C (Interactive Trust & Identity Storage)
**Status:** Complete / Verified
**Goal:** User physical device verification of the final visual UI constraints. Physical verification of pairing flow, SAS exchange, connection transitions, and Android key storage. Linux and Android now support an interactive pairing flow requiring explicit user acceptance. Android securely stores Ed25519 identity keys in the AndroidKeyStore using hardware backing. Linux stores trusted devices in an SQLite database. Both clients have proper UX for pairing, including displaying the SAS code and allowing users to Accept or Reject. All Linux and Android tests pass cleanly.

---

## 2. Architecture & Protocol Summary

* **Protocol Version**: `dev.ferry.v1` (2-byte `FY` magic + 4-byte big-endian uint32 payload length + JSON envelope).
* **Discovery Service Type**: `_ferry._tcp.local.` (Linux) / `_ferry._tcp` (Android).
* **Discovery TXT Attributes**: `v=1`, `id=<UUIDv4>`, `name=<display_name>`, `type=desktop|mobile`, `os=archlinux|android`, `port=53770`, `app_version=0.1.0`.
* **Linux Stack**: Python 3.14 + `zeroconf.asyncio` (`AsyncZeroconf`) + `PyGObject` (GTK4 + Libadwaita 1) + `asyncio` + SQLite + `cryptography` (50.0.1).
* **Android Stack**: Kotlin 2.1 + `android.net.nsd.NsdManager` + Jetpack Compose + Material 3 + Android SDK 35/36 + platform `javax.crypto`.
* **Security Model**: Custom application-layer AKE over TCP. Ed25519 identity keys (pinned in trust store), X25519 ephemeral DH, HKDF-SHA256 session key and SAS derivation, and ChaCha20-Poly1305 AEAD framing.

---

## 3. Implemented & Verified Functionality

* [x] **Project Repository & Configuration**: Standardized structure, `.gitignore`, `README.md`, and agent operating manual (`AGENTS.md`).
* [x] **Comprehensive Documentation Suite**: `ARCHITECTURE.md`, `PROTOCOL.md`, `SECURITY.md`, `DEVELOPMENT.md`, `TESTING.md`, `DECISIONS.md`, `PROJECT_STATE.md`, `PHASE_3C_REPORT.md`, `PHASE_3C.1_PHYSICAL_VERIFICATION.md`, `PHASE_3C.2_FINAL_ACCEPTANCE.md`.
* [x] **Linux Discovery Subsystem (`linux/src/ferry_linux/core/discovery.py`)**
* [x] **Android Discovery Subsystem (`android/app/src/main/kotlin/dev/ferry/app/discovery/`)**
* [x] **Physical End-to-End Discovery Verification (Phase 2A)**
* [x] **Linux Secure Control Plane (Phase 2B)**
* [x] **Android Secure Control Plane (Phase 2B)**
* [x] **Interactive Trust & Android KeyStore (Phase 2C / 2C.3)**
* [x] **Secure Transfer Transport & In-Band Multiplexing (Phase 3A / 3A.1)**
* [x] **Linux File Receiver & Transfer Approvals (Phase 3B / 3B.1)**
* [x] **Complete User-Facing File Transfer (Phase 3C / 3C.1)**:
  - Linux `Gtk.FileDialog` integration, header Send button, per-device send button, live progress bars, transfer history panel.
  - Android SAF `GetContent` file picker FAB, memory-efficient `InputStream` streaming, Compose live progress and history cards.
  - Bidirectional physical transfers verified end-to-end with SHA-256 integrity, rejection, in-flight cancellation, and trusted reconnects.
* [x] **Final Acceptance Gate Verification (Phase 3C.2)**:
  - 0-byte file transfers supported and physically validated end-to-end on real hardware.
  - User-facing UI Cancel buttons added to Linux GTK4 and Android Compose progress cards; wire `TRANSFER_CANCEL` and clean `.part` purge verified mid-flight.
* [x] **Transfer Reliability & Error Recovery (Phase 3D)**:
  - `TransferError` exception class for filesystem/IO failures during transfers.
  - `IncomingTransfer.begin()`, `receive_chunk()`: OS errors caught, `.part` cleaned, state set to FAILED before re-raising.
  - `IncomingTransfer.cancel()`: safe from any state (IDLE, ACCEPTED, TRANSFERRING, COMPLETED).
  - `OutgoingTransfer.stream_chunks()`: guards `open()` and `read()` against `OSError`.
  - Session-disconnect cleanup: `_cancel_incoming_transfer_for_peer()` and `_cancel_outgoing_transfer_for_peer()` called in `_run_session_loop` finally block.
  - 120s acceptance timeout: incoming transfers auto-rejected if user doesn't respond.
  - Idempotent history recording via `_record_transfer_once()` — prevents duplicate DB rows.
  - Duplicate TRANSFER_COMPLETE / TRANSFER_CANCEL messages are silently ignored.
  - UI transfer rows cleared on session disconnect with "Connection lost" label.
  - Stale `.part` file cleanup on service `start()`.
  - Android: session-dropout records FAILED history for interrupted incoming transfers.
  - Android: `pendingTransferAccept.complete(false)` on session exit.
  - Android: duplicate ACCEPT/REJECT/CANCEL/COMPLETE messages guarded via `handledTransferIds` set.
  - Android: `appendHistory()` deduplicates by transfer_id.
  - **161 Linux tests / 57 Android tests — all passing.**
* [x] **Physical Reliability Acceptance Verification (Phase 3D.1)**:
  - **PT-1** (Disconnect mid-transfer) validated on real hardware: `FAILED` state reached, `.part` purged.
  - **PT-2** (User cancellation) validated on real hardware: `CANCELLED` state reached, `.part` purged.
  - **PT-3** (Recovery/Retry) validated on real hardware: New transfer ID generated, completed successfully with exact 55 MiB file size and matching SHA-256 hash.
* [x] **Persistent Interrupted Transfer Foundation (Phase 3E Task 1)**:
  - SQLite schema migration to v3 with 7 new resume columns.
  - `INTERRUPTED` state in `TransferState` machine.
  - `IncomingTransfer.interrupt()` safe file handle closure and `.part` file retention.
  - `InterruptedTransferInfo` and database persistence helpers.
* [x] **Resumable Transfer Protocol (Phase 3E Task 2)**:
  - Wire protocol models: `TRANSFER_RESUME_REQUEST`, `TRANSFER_RESUME_ACCEPT`, `TRANSFER_RESUME_REJECT`.
  - Comprehensive payload validation: UUIDs, bounds, 64-char lowercase hex SHA-256, chunk alignment.
  - `IncomingTransfer.prepare_resume_request()`: disk-authoritative sizing, boundary validation, streaming SHA-256 computation.
  - Minimal service-layer message dispatch recognizing resume control frames.
* [x] **Resume Execution & UI (Phase 3E Task 3)**:
  - Wired `RESUME_REQUESTED` and `RESUMING` state transitions.
  - Sender-side source file verification (`st_mtime`/`st_size` check on Linux, length check on Android).
  - Sender-side streaming partial prefix SHA-256 hashing.
  - Chunk frame seq-number resuming and stream skipping.
  - Android `InterruptedTransferStore` SharedPreferences backing.
  - Interactive "Resume" and "Discard" UI buttons implemented natively on both GTK4 and Compose.
  - **219/219 Linux tests passing; Android tests passing.**
  - **Physical Verification:** Android resume path verified. Linux resume path has a known issue (deferred). Existing non-resume transfers remain fully functional.
* [x] **UI Redesign & Final Visual Polish (Complete)**:
  - Diagnostic and Clipboard UI elements intentionally removed to achieve minimal design goals.
* [x] **Multi-File & Directory Transfers (Phase 4C)**:
  - Android `sendBatch()` sends `BATCH_REQUEST` with `total_items` and waits for `BATCH_ACCEPT` before streaming individual files.
  - Android `onBatchRequest()` bug fixed — now properly auto-accepts incoming batches and tracks `activeBatchId`.
  - Android `onTransferRequest()` respects `activeBatchId` — batched items skip the "busy" guard.
  - Android `onBatchCancel()` / `onBatchComplete()` clear active batch state.
  - Linux `send_batch()` sends batch metadata, iterates files sequentially, sends `BATCH_COMPLETE`.
  - Linux GTK4 "Send Folder" button in header bar via `Gtk.FileDialog.select_multiple_folders`.
* [x] **Desktop Notifications (MVP Final Sprint)**:
  - Linux `NotificationManager` wrapping `GLib.Notification`.
  - Fires on incoming transfer request and transfer completion.
  - Wired into `FerryService` and `FerryApplication`.
* [x] **System Integration (MVP Final Sprint)**:
  - `linux/systemd/ferry.service` — systemd user unit for headless daemon.
  - `linux/desktop/dev.ferry.Ferry.desktop` — XDG `.desktop` entry for GNOME launcher.
  - `linux/install_integration.sh` — interactive installer/uninstaller.
* [x] **MVP Completion Sprint** — all features implemented and builds verified:
  - **Android recursive SAF folder traversal**: `collectUrisRecursively()` now walks all subdirectories depth-first, replacing the shallow 1-level loop.
  - **`ACTION_SEND_MULTIPLE` share intent**: Ferry appears in the Android Share Sheet when sharing multiple files at once; all URIs are sent as a batch automatically on connect.
  - **Android Foreground Service** (`FerryTransferService`): transfers survive app backgrounding; sticky progress notification shows file name and percent complete; auto-started on transfer begin, auto-stopped on completion.
  - **Batch progress overlay**: `TransferProgress.BatchInfo` added; status text shows "Sending batch 2/5: photo.jpg (73%)" during multi-file sends.
  - **Linux drag-and-drop**: GTK4 `Gdk.FileList` drop target on the main scrolled window; single-file drops send directly; multi-file/folder drops use `send_batch`.
  - **Linux "Send Files" multi-select button**: `Gtk.FileDialog.open_multiple` lets users pick multiple individual files to send as a batch without needing a folder.
- **Settings dialog**: Gear button in header bar shows current config (device name, port, download dir, auto-accept).
  - **Diagnostics expander**: Collapsible row in System Status shows local identity key prefix, connected peer name/address, and mDNS status.
  - **`--send FILE` CLI flag**: `python3 -m ferry_linux --send /path/to/file` queues file(s) for sending; fires when a peer connects.
  - **Nautilus script** (`linux/scripts/nautilus/send_via_ferry.sh`): Right-click → Scripts → Send via Ferry integration.
  - **`FerryService.config`/`identity` public**: Settings and diagnostics now read directly from service.
  - **Stale "Phase 3C" UI label removed** from status banner.
  - **Linux GTK State Propagation Bug Fixed**: Corrected the `IPC_SESSION_UPDATE` payload to include identity fields (`remote_device_id`), resolving the issue where dynamically established connections failed to show the "Connected" label and "Send Files" / "Send Folder" actions for trusted devices.
  - **219/219 Linux tests pass** (1 pre-existing port-reuse flaky). **Android `assembleDebug` + `testDebugUnitTest` both succeed.**

---

## 4. Development Environment

* **Host OS**: Arch Linux x86_64 (Linux 7.1.9-arch1-2)
* **Desktop Environment**: GNOME Shell 50.4 (Wayland)
* **Python**: Python 3.14.7 (`gtk4`, `libadwaita`, `python-gobject`, `zeroconf` 0.151.1)
* **Java**: OpenJDK 26.0.2.1 (`java-26-openjdk`)
* **Android Toolchain**: Android SDK Platform 35/36 at `/home/sanjeet/Android/Sdk`, Build-Tools 35/36/37
* **Gradle**: Gradle 9.4.1 with Android Gradle Plugin 8.8.2, Kotlin 2.1.10
* **Connected Physical Device**: Realme RMX3870 (Android 16 / SDK 36, Device ID `QO8L696L6LYLUC45`) via USB ADB & Wi-Fi

---

## 5. Build, Test, and Run Commands

### Linux
```bash
# Run all unit and integration tests (204 tests)
PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v

# Run desktop UI with discovery active
PYTHONPATH=linux/src python3 -m ferry_linux

# Run headless daemon service
PYTHONPATH=linux/src python3 -m ferry_linux --service
```

### Android
```bash
# Run unit tests (57 tests)
cd android && ./gradlew testDebugUnitTest

# Build debug APK
cd android && ./gradlew assembleDebug

# Install APK to connected device
adb install -r android/app/build/outputs/apk/debug/app-debug.apk

# Launch app on connected device
adb shell am start -n dev.ferry.app/.MainActivity
```

---

## 6. Known Limitations & Phase Boundary

* **Single Transfer at a Time**: Current architecture handles one active transfer at a time per peer session.
* **No Chunk Stall Enforcement**: `TRANSFER_CHUNK_TIMEOUT_SECS = 120.0` is defined; last-chunk-time tracking is in place, but active per-transfer stall enforcement (a periodic asyncio task) is not yet implemented. The existing 300s session idle timeout remains the backstop.
* **Linux Resume Path**: Linux-originated resume (Linux as receiver) has a known issue, deferred from Phase 3E. Existing non-resume transfers remain fully functional on both platforms.
* **Settings Write-Back**: The Settings dialog (gear button) currently shows current config read-only. Write-back (editing device name, port, download dir) is a future task.

---

## 7. Next Recommended Task

**Physical Device QA Session** — connect Android device (ADB or Wi-Fi) and verify the following matrix:

| # | Test | Expected |  
|---|------|----------|
| 1 | mDNS discovery | Android auto-discovers Linux peer within 5s |
| 2 | Single file: Linux → Android | Transfer completes, file appears in Downloads/Ferry |
| 3 | Single file: Android → Linux | Transfer completes, SHA-256 verified |
| 4 | Multi-file: Share 3 files from Files app → Ferry | All 3 files received, batch progress shows "2/3" |
| 5 | Folder drag-and-drop onto Linux Ferry window | Batch send initiated, recursive subfolder files included |
| 6 | Background transfer: start transfer, background app | Transfer continues, foreground notification visible |
| 8 | Desktop notification | Incoming transfer on Linux shows GNOME notification |
| 9 | Systemd service | Run `linux/install_integration.sh`, log out/in, verify daemon starts |
| 10 | Nautilus script | Right-click → Scripts → "Send via Ferry", file sends |


## Phase 5: Notification Mirroring (Pending Physical QA)
- `NotificationListenerService` pipeline implemented.
- `NotificationBridge` (DBus org.freedesktop.Notifications) implemented.
- Protocol expanded with `CAPABILITIES` and `NOTIFICATION_POST/REMOVE`.
- UI pairing fixed to remove 'Reject' when only in 'PAIRING' state.
- **Next Step:** User must perform physical device validation of notifications and pairing.

## Phase 5a: Pairing State Machine Bug Fix (Pending Physical QA)
- Android pairing flow incorrectly prompted for local authorization upon receiving Linux's `PAIR_DECISION("ACCEPT")`.
- `FerryControlClient.kt` was fixed to transition directly to `ESTABLISHED` and echo `ACCEPT` without entering `WAITING_FOR_LOCAL_DECISION`.
- Linux `service.py` was updated to advertise capabilities (`notify.v1`) on fresh pairing, not just reconnects.
