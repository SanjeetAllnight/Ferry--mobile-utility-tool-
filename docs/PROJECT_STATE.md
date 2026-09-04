# Ferry Project State & Persistent Context

This document is the primary persistent context file for **Ferry**. It reflects the factual, verified state of the codebase.

---

### Current Phase: Phase 3 (File Transfer Execution)
**Status:** In Progress — Phase 3C Fully Closed (Phase 3C.2 Acceptance Gates Passed)
**Goal:** Implement file transfer capabilities between paired devices.

### Phase 3 Progress Tracking:
- **Phase 3A (Transfer Architecture & Base Data Channel)**: Complete / Verified. TransferState and metadata tracking implemented on both OS platforms. In-band multiplexing via binary `FYCH` chunk frames established over existing ChaCha20-Poly1305 sessions (ADR 007). All unit tests passing (127 Linux, 50 Android). Physical Android ↔ Linux streaming and SHA-256 integrity verified over local Wi-Fi.
- **Phase 3B (Linux File Receiving & Transfer Approvals)**: Complete / Verified. `IncomingTransfer` refactored to parse directly to user's specified `download_dir`. Receiver `FerryService` extended to surface `TRANSFER_REQUEST` up to the UI. Fully integration tested on Linux and physically verified receiving a file from Android over Wi-Fi.
- **Phase 3C / 3C.1 (Sender & Receiver UI / File Picking & Physical QA)**: Complete / Fully Verified. Full bidirectional user-facing file transfer experience physically verified on real Android device and Arch Linux desktop. Linux `Gtk.FileDialog` and Android SAF `GetContent` picker validated. Bidirectional streaming (up to 16.5 MB at ~11.8 MB/s), SHA-256 integrity verification, live UI progress, incoming transfer approval, and transfer history panels verified.
- **Phase 3C.2 (Final Acceptance-Gate Verification & Discrepancy Resolution)**: Complete / Fully Verified. Resolved both remaining acceptance gate discrepancies: (1) native zero-byte (0-byte) file transfer support with SHA-256 `e3b0c442...` integrity verification, and (2) user-facing in-flight transfer cancellation UI buttons on GTK4 and Jetpack Compose with wire `TRANSFER_CANCEL` transmission, immediate `.part` cleanup, and `CANCELLED` history recording. 149/149 Linux unit tests and 52/52 Android unit tests passing (201 total). Phase 3C is officially CLOSED.

### Previous Phase: Phase 2C (Interactive Trust & Identity Storage)
**Status:** Complete / Verified
**Goal:** Physical verification of pairing flow, SAS exchange, connection transitions, and Android key storage. Linux and Android now support an interactive pairing flow requiring explicit user acceptance. Android securely stores Ed25519 identity keys in the AndroidKeyStore using hardware backing. Linux stores trusted devices in an SQLite database. Both clients have proper UX for pairing, including displaying the SAS code and allowing users to Accept or Reject. All Linux and Android tests pass cleanly.

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
# Run all unit and integration tests (149 tests)
PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v

# Run desktop UI with discovery active
PYTHONPATH=linux/src python3 -m ferry_linux

# Run headless daemon service
PYTHONPATH=linux/src python3 -m ferry_linux --service
```

### Android
```bash
# Run unit tests (52 tests)
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

* **No Resumption (Phase 3D)**: File transfer resumption for interrupted transfers is planned for Phase 3D.
* **Single Transfer at a Time**: Current architecture handles one active transfer at a time per peer session.

---

## 7. Next Recommended Task

* **Phase 3D (Transfer Optimization, Error Recovery & Resumption)** or transition to next planned milestone.
