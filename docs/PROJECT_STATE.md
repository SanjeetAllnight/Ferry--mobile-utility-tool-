# Ferry Project State & Persistent Context

This document is the primary persistent context file for **Ferry**. It reflects the factual, verified state of the codebase.

---

### Current Phase: Phase 3 (File Transfer Execution)
**Status:** Pending Start
**Goal:** Implement file transfer capabilities between paired devices.

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
* [x] **Comprehensive Documentation Suite**: `ARCHITECTURE.md`, `PROTOCOL.md`, `SECURITY.md`, `DEVELOPMENT.md`, `TESTING.md`, `DECISIONS.md`, `PROJECT_STATE.md`, `PHASE_2B_AUDIT.md`, and `PHASE_2B.1_SECURITY_REVIEW.md`.
* [x] **Linux Discovery Subsystem (`linux/src/ferry_linux/core/discovery.py`)**:
  * `AsyncZeroconf` service advertisement with dynamic local IP enumeration and standard Ferry TXT attributes.
  * `AsyncServiceBrowser` and `AsyncServiceInfo` peer resolution.
  * In-memory deduplication by stable `device_id` and self-advertisement filtering.
  * Event listener callbacks for device appearance and removal.
  * Live dynamic "Nearby Devices" display in GTK4 / Libadwaita window (`linux/src/ferry_linux/ui/window.py`).
  * Automated unit tests (`test_discovery_models.py`, `test_discovery_manager.py`) and live mDNS loopback test (`test_discovery_integration.py`).
* [x] **Android Discovery Subsystem (`android/app/src/main/kotlin/dev/ferry/app/discovery/`)**:
  * `NsdManager` service registration and discovery engine (`FerryDiscoveryEngine.kt`).
  * MulticastLock management for background/foreground packet reception.
  * Thread-safe `StateFlow<List<DiscoveredDevice>>` emitting resolved peers.
  * Dynamic Compose UI in `FerryApp.kt` rendering discovered Arch Linux hosts.
  * Automated unit tests (`DiscoveredDeviceTest.kt`).
* [x] **Physical End-to-End Discovery Verification (Phase 2A)**:
  * Bidirectional discovery verified: Linux discovered Android, Android discovered Linux.
  * Verified service stop/removal handling and duplicate filtering.
* [x] **Linux Secure Control Plane (Phase 2B)**:
  * `IdentityManager` for Ed25519 keys and signing (`identity.key` with mode `0600`).
  * `FerrySession` state machine, X25519 DH, HKDF SAS, and ChaCha20-Poly1305 AEAD framing.
  * Async TCP server and client connecting to discovered peers.
  * 53 passing automated unit/integration tests in `linux/tests/`.
* [x] **Android Secure Control Plane (Phase 2B)**:
  * Handshake, HKDF SAS derivation, and ChaCha20-Poly1305 framing working.
  * Verified unit tests (`SessionTest.kt`, `CryptoTest.kt`, `ControlClientTest.kt`).
* [x] **Interactive Trust & Android KeyStore (Phase 2C)**:
  * **Android**: Ed25519 identity now correctly implemented using `AndroidKeyStore` (`KeyGenParameterSpec`), guaranteeing secure hardware-backed storage where available.
  * **Android**: Interactive UI for incoming connections displaying the 6-digit SAS code, allowing Accept/Reject.
  * **Linux**: `TrustedDevices` SQLite table accurately tracking previously authenticated peers.
  * **Linux**: Adw.MessageDialog prompting user to confirm pairing codes before advancing to `ESTABLISHED`.
  * **Both**: Interactive handshake verified to halt at `PAIRING` state. Upon explicit acceptance by both parties, connection progresses to `ESTABLISHED`. State transitions verified via full unit test coverage.
* [x] **Physical End-to-End Control Plane Verification (Phase 2B/2C)**:
  * Android (`Realme RMX3870`, Android 16/SDK 36) connected to Arch Linux daemon (`archnoir`) over local Wi-Fi.
  * Mutual AKE completed, SAS computed (`049968`), Ed25519 signatures verified, and AEAD session reached `ESTABLISHED` with `● Secure` badge in UI.

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
# Run all unit and integration tests (53 tests)
PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v

# Run desktop UI with discovery active
PYTHONPATH=linux/src python3 -m ferry_linux

# Run headless daemon service
PYTHONPATH=linux/src python3 -m ferry_linux --service
```

### Android
```bash
# Run unit tests
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

* **No File Transfer Engine (Phase 3)**: Data streaming channels and SAF file I/O will follow after pairing UX is complete.

---

## 7. Next Recommended Task

* **Begin Phase 2C (Trust & Pairing UX, Keystore Migration)**:
  * Implement GNOME-style desktop notification / Libadwaita dialog and Compose pairing UI for interactive SAS verification.
  * Implement mutual `PAIRING_DECISION` acceptance flow before persisting trust.
  * Migrate Android Ed25519 identity key generation to hardware-backed `AndroidKeyStore`.

