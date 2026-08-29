# Ferry Project State & Persistent Context

This document is the primary persistent context file for **Ferry**. It reflects the factual, verified state of the codebase.

---

## 1. Project Phase & Milestone

* **Current Phase**: **Phase 1 — Project Foundation & Architecture**
* **Current Milestone**: **M1.0 (Foundation, Toolchain, Skeletons, and Agent Operating Manual)**
* **Project Status**: Skeletons established, toolchains verified, builds and tests operational on Linux and Android.

---

## 2. Architecture & Protocol Summary

* **Protocol Version**: `dev.ferry.v1` (2-byte `FY` magic + 4-byte big-endian uint32 payload length + JSON envelope).
* **Linux Stack**: Python 3.14 + `PyGObject` (GTK4 + Libadwaita 1) + `asyncio` + SQLite.
* **Android Stack**: Kotlin 2.1 + Jetpack Compose + Material 3 + Android SDK 35/36.
* **Security Model**: LAN is untrusted. Mutual Ed25519 identity, SAS out-of-band numeric PIN pairing, TLS 1.3 / ChaCha20-Poly1305 transport, SHA-256 integrity verification.

---

## 3. Implemented Functionality (Phase 1)

* [x] **Project Repository & Configuration**: Standardized structure, `.gitignore`, `README.md`, and agent operating manual (`AGENTS.md`).
* [x] **Comprehensive Documentation Suite**: `ARCHITECTURE.md`, `PROTOCOL.md`, `SECURITY.md`, `DEVELOPMENT.md`, `TESTING.md`, `DECISIONS.md`, and `PROJECT_STATE.md`.
* [x] **Linux Application & Service Skeleton**:
  * Python packaging (`pyproject.toml`).
  * XDG directory and configuration manager (`core/config.py`).
  * SQLite persistence manager for trusted peers and transfer logs (`core/db.py`).
  * Asyncio Ferry core service skeleton (`core/service.py`).
  * Protocol v1 binary framer and message models (`protocol/models.py`).
  * GTK4 / Libadwaita desktop UI (`ui/app.py`, `ui/window.py`).
  * Unit test suite (`tests/`).
* [x] **Android Application Skeleton**:
  * Gradle build configuration (`settings.gradle.kts`, `build.gradle.kts`, `app/build.gradle.kts`, `gradle/libs.versions.toml`).
  * Self-contained Gradle 9.4.1 wrapper.
  * Jetpack Compose UI with Ferry branding and Phase 1 status card (`dev.ferry.app.ui.FerryApp`).
  * Protocol v1 constants and model contracts (`dev.ferry.app.protocol.ProtocolConstants`).
  * Unit test suite (`app/src/test/`).

---

## 4. Development Environment

* **Host OS**: Arch Linux x86_64 (Linux 7.1.9-arch1-2)
* **Desktop Environment**: GNOME Shell 50.4 (Wayland)
* **Python**: Python 3.14.7 (`gtk4`, `libadwaita`, `python-gobject` available)
* **Java**: OpenJDK 26.0.2.1 (`java-26-openjdk`)
* **Android Toolchain**: Android SDK Platform 35/36 at `/home/sanjeet/Android/Sdk`, Build-Tools 35/36/37
* **Gradle**: Gradle 9.4.1 with Android Gradle Plugin 8.8.2, Kotlin 2.1.10
* **Connected Device**: Realme RMX3870 (Android 16 / SDK 36, Device ID `QO8L696L6LYLUC45`) via ADB

---

## 5. Build, Test, and Run Commands

### Linux
```bash
# Run unit tests
python3 -m unittest discover -s linux/tests -v

# Run desktop UI
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

## 6. Known Limitations & Phase 1 Boundary

* **No Active Discovery**: mDNS discovery will be implemented in Phase 2.
* **No Active Pairing**: SAS cryptographic exchange will be implemented in Phase 2.
* **No File Transfer Engine**: Binary streaming, chunking, and SAF I/O will be implemented in Phase 3.

---

## 7. Next Recommended Task

* **Phase 2: Local Discovery & Cryptographic Pairing**
  * Implement mDNS announcer & listener (`zeroconf` on Linux, `NsdManager` on Android).
  * Implement cryptographic handshake with Ed25519 / X25519 and SAS numeric PIN comparison UI on both GNOME and Android Compose.
