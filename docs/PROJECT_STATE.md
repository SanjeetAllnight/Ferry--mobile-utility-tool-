# Ferry Project State & Persistent Context

This document is the primary persistent context file for **Ferry**. It reflects the factual, verified state of the codebase.

---

## 1. Project Phase & Milestone

* **Current Phase**: **Phase 2A — Local Device Discovery (Physical E2E Verified)**
* **Current Milestone**: **M2.0 (mDNS/DNS-SD Peer Discovery via AsyncZeroconf & NsdManager)**
* **Project Status**: Skeletons operational, discovery engine implemented and physically verified between Arch Linux and Android over Wi-Fi with automated unit/integration tests passing.

---

## 2. Architecture & Protocol Summary

* **Protocol Version**: `dev.ferry.v1` (2-byte `FY` magic + 4-byte big-endian uint32 payload length + JSON envelope).
* **Discovery Service Type**: `_ferry._tcp.local.` (Linux) / `_ferry._tcp` (Android).
* **Discovery TXT Attributes**: `v=1`, `id=<UUIDv4>`, `name=<display_name>`, `type=desktop|mobile`, `os=archlinux|android`, `port=53770`, `app_version=0.1.0`.
* **Linux Stack**: Python 3.14 + `zeroconf.asyncio` (`AsyncZeroconf`) + `PyGObject` (GTK4 + Libadwaita 1) + `asyncio` + SQLite.
* **Android Stack**: Kotlin 2.1 + `android.net.nsd.NsdManager` + Jetpack Compose + Material 3 + Android SDK 35/36.
* **Security Model**: LAN is untrusted. Discovery only establishes peer availability; discovered peers are unauthenticated until explicit cryptographic pairing in Phase 2C.

---

## 3. Implemented & Verified Functionality

* [x] **Project Repository & Configuration**: Standardized structure, `.gitignore`, `README.md`, and agent operating manual (`AGENTS.md`).
* [x] **Comprehensive Documentation Suite**: `ARCHITECTURE.md`, `PROTOCOL.md`, `SECURITY.md`, `DEVELOPMENT.md`, `TESTING.md`, `DECISIONS.md`, and `PROJECT_STATE.md`.
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
* [x] **Physical End-to-End Discovery Verification**:
  * Physical Android device (`Realme RMX3870`, Android 16 / SDK 36) connected on `10.213.207.31` over Wi-Fi.
  * Arch Linux host (`archnoir`) connected on `10.213.207.51` over Wi-Fi.
  * Bidirectional discovery verified: Linux discovered `RMX3870 (Ferry)` at `10.213.207.31:53770`, Android discovered and rendered `archnoir (Ferry)` at `10.213.207.51:53770`.
  * Verified service stop/removal handling, service restart rediscovery, and app restart rediscovery with zero duplicates.

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
# Run all unit and integration tests (20 tests)
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

* **No Cryptographic Pairing (Phase 2B/2C)**: Discovered peers are strictly untrusted. SAS numeric PIN exchange and key storage are scheduled for Phase 2B/2C.
* **No File Transfer Engine (Phase 3)**: Data streaming channels and SAF file I/O will follow after pairing.

---

## 7. Next Recommended Task

* **Phase 2B: Control Plane Connection & Handshake Architecture**
  * Establish authenticated TCP control channel over TLS 1.3 / Noise protocol.
  * Implement ephemeral X25519 key exchange between discovered peers.
