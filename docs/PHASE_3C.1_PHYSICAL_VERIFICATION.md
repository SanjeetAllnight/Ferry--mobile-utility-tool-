# Phase 3C.1 Physical Verification Report

**Status:** PHASE 3C FULLY VERIFIED
**Date:** 2026-08-30
**Verification Type:** End-to-End Real Device & Desktop UI Interaction

---

## 1. Test Environment

| Attribute | Specification |
|---|---|
| **Linux Host** | Arch Linux x86_64, Linux 7.1.9-arch1-2, GNOME Shell 50.4 (Wayland) |
| **Linux Application** | Python 3.14.7, GTK 4.0 / Libadwaita 1 (`python-gobject`), AsyncZeroconf, SQLite |
| **Android Device** | Physical Realme RMX3870 (Android 16 / SDK 36, Device ID `QO8L696L6LYLUC45`) |
| **Android Application** | Kotlin 2.1.10, Jetpack Compose Material 3, Android SDK 35/36 |
| **Local Network** | Local Wi-Fi (`10.213.207.x`) |
| **Transport** | Framed AEAD ChaCha20-Poly1305 over TCP (`dev.ferry.v1`) |

---

## 2. Verification Results Summary

| Test Case | Description | Verification Type | Result |
|---|---|---|---|
| **Test 1: Linux → Android** | Real file selected in GTK picker, transferred, and verified on Android | **PHYSICALLY VERIFIED** | **PASS** |
| **Test 2: Android → Linux** | Real files picked via Android SAF, accepted in Linux UI, saved to `~/Downloads/Ferry/` | **PHYSICALLY VERIFIED** | **PASS** |
| **Test 3: Rejection** | Transfer initiated, Linux user clicked Reject in dialog; cleanly aborted without temp file | **PHYSICALLY VERIFIED** | **PASS** |
| **Test 4: Cancellation** | In-flight 15 MB transfer interrupted mid-stream; socket disconnect cleanly deletes `.part` | **PHYSICALLY VERIFIED** | **PASS** |
| **Test 5: History** | Transfer history presented in UI (Linux & Android) and SQLite database | **PHYSICALLY VERIFIED** | **PASS** |
| **Test 6: Trusted Reconnect** | Disconnect and reconnect without pairing prompt; reaches ESTABLISHED directly | **PHYSICALLY VERIFIED** | **PASS** |
| **Test 7: Filename Safety** | Filename with spaces, Unicode emojis, and multiple dots preserved cleanly | **PHYSICALLY VERIFIED** | **PASS** |
| **Test 8: Zero-Byte File** | 0-byte file rejected by metadata validator as designed; UI displays failed without crash | **PHYSICALLY VERIFIED** | **PASS** |
| **Automated Suite** | Full Linux unit tests (147 tests) & Android unit tests (50 tests) | **AUTOMATED** | **PASS (100%)** |

---

## 3. Detailed Physical Test Evidence & Hashes

### Test 1: Linux → Android Transfer
- **File**: `test_linux_to_android.txt` (10,800 bytes)
- **Sender**: Linux Desktop (`Gtk.FileDialog` → `service.send_file()`)
- **Receiver**: Android Ferry App (`FerryTransferReceiver`)
- **Android Location**: `/sdcard/Download/Ferry/staging/test_linux_to_android.txt`
- **Sender SHA-256**: `96b2717639b2b0c42cb38f78c842b06ddc1491203a76ce4fda5a8cceabf24dd7`
- **Receiver SHA-256**: `96b2717639b2b0c42cb38f78c842b06ddc1491203a76ce4fda5a8cceabf24dd7`
- **Integrity**: **MATCH (100%)**

### Test 2: Android → Linux Transfer (Small & Large Media)
1. **Text File**: `test_android_to_linux.txt` (16,200 bytes)
   - **Sender**: Android SAF (`ActivityResultContracts.GetContent`)
   - **Receiver**: Linux Ferry (`~/Downloads/Ferry/test_android_to_linux.txt`)
   - **Sender SHA-256**: `2004f0fa9ca6003be6e937376ad3a5056062d1ff3ba9c3d937603e799da4c966`
   - **Receiver SHA-256**: `2004f0fa9ca6003be6e937376ad3a5056062d1ff3ba9c3d937603e799da4c966`
   - **Integrity**: **MATCH (100%)**
2. **Large Media Image**: `ABC_0196.JPG` (16,479,698 bytes / 15.7 MB)
   - **Receiver**: Linux Ferry (`~/Downloads/Ferry/ABC_0196.JPG`)
   - **Receiver SHA-256**: `065ee2636e4a8df541b1e05dce97030ca74bf6170ad3a2047c0eb741082a1594`
   - **Transfer Duration**: ~1.4 seconds over Wi-Fi
   - **Measured Throughput**: **~11.8 MB/s**

### Test 3: Transfer Rejection
- **File**: `log_list.json` (33,644 bytes) sent from Android.
- **Action**: Linux user clicked **Reject** on `Adw.MessageDialog`.
- **Sender Log**: `TRANSFER_REJECT received for cd554788 — Transfer rejected by peer`.
- **Filesystem**: Verified 0 bytes created in `~/Downloads/Ferry/`; temporary files cleaned up.

### Test 4: In-Flight Cancellation
- **File**: `test_large_payload.bin` (15,728,640 bytes) streaming from Linux.
- **Action**: Android disconnected mid-stream (~393 KB transferred).
- **Receiver Cleanup**: `finally` block in `runEncryptedSessionLoop` triggered `activeReceiver?.cancel()`.
- **Filesystem**: Verified `.part` file removed, no finalized file left behind.

### Test 5: Transfer History
- **Linux**: Libadwaita PreferencesGroup dynamically listed all recent transfers with filenames, direction arrows, sizes, and completion status icons.
- **Database**: SQLite `transfer_history` table correctly records `transfer_id`, `direction`, `status`, `sha256`, and timestamps.
- **Android**: Compose `TransferHistoryCard` displays recent transfers.

### Test 6: Trusted Reconnect
- **Action**: Tapped Disconnect, then tapped Connect on Android.
- **Handshake**: Handshake completed, mutual signature verified against trust store, session directly transitioned to `ESTABLISHED` without prompting SAS pairing codes.

### Test 7: Filename Safety (Unicode, Spaces & Dots)
- **File**: `test 🚀 ferry..data.txt` (2,900 bytes)
- **Receiver Location**: `/sdcard/Download/Ferry/staging/test 🚀 ferry..data.txt`
- **Sender SHA-256**: `e8589376d93aa495dc7173913efa295456ff9bd2af3beefa25ac1e589a806ba2`
- **Receiver SHA-256**: `e8589376d93aa495dc7173913efa295456ff9bd2af3beefa25ac1e589a806ba2`
- **Result**: Spaces, emojis, and double dots preserved safely.

### Test 8: Zero-Byte File
- **File**: `test_zero_byte.bin` (0 bytes)
- **Behavior**: `OutgoingTransfer.build_metadata()` and `TransferMetadata.fromMap()` enforce `file_size > 0`.
- **Result**: Transfer rejected gracefully with error code; UI updated without crashing.

---

## 4. Bugs Identified & Resolved During Physical QA

1. **Android Activity Lifecycle Disconnection Bug (`MainActivity.kt`)**:
   - *Problem*: `onStop()` called `controlClient.disconnect()`. Opening the system document picker put MainActivity into the background, inadvertently severing the active connection before the file could be selected.
   - *Fix*: Moved disconnect to `onDestroy()` checking `isFinishing`.
2. **Linux Send Button Sensitivity with Established Sessions (`window.py`)**:
   - *Problem*: `_update_trusted_devices` checked `devices` list in SQLite first and returned early with `self._send_btn.set_sensitive(False)` if DB was empty, ignoring active in-memory established sessions.
   - *Fix*: Updated `_update_trusted_devices` to compute sensitivity directly from `bool(established)` and include active established sessions in display rows.
3. **One-Way Pairing Echo on Pre-Trusted Peer (`FerryControlClient.kt`)**:
   - *Problem*: When Android was already established and Linux prompted pairing, Linux sent `PAIR_DECISION: ACCEPT` and waited for remote echo. Android was already established so it did not reply.
   - *Fix*: Added echo reply in `FerryControlClient` when receiving `PAIR_DECISION: ACCEPT` while in `ESTABLISHED` state.
4. **Cancellation Temp File Cleanup in Session Loop (`FerryControlClient.kt`)**:
   - *Problem*: If an in-flight transfer was cancelled by socket drop, `activeReceiver` was not cancelled in the `runEncryptedSessionLoop` finally block.
   - *Fix*: Added explicit `activeReceiver?.cancel()` in `finally`.

---

## 5. Physical UI Quality & UX Review

- **Linux**:
  - Send File header button is distinct and prominent when connected.
  - Per-device Send button on connected rows provides a clear, natural path to send to specific targets.
  - Active Transfers progress bar provides clear live feedback.
  - Transfer History list gives instant confirmation of delivered files.
  - Dialogs are clean Adwaita style with destructive/suggested action styling.
- **Android**:
  - Extended FAB ("Send File") is accessible and clean.
  - Material 3 progress card with linear indicator shows live percentages.
  - System document picker opens smoothly and seamlessly streams files.

---

## 6. Final Status

**PHASE 3C FULLY VERIFIED**
All user-facing file-transfer flows on Linux and Android have been physically validated end-to-end on real hardware.
