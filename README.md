# Ferry ⛴️

**Ferry** is a local, privacy-first Android ↔ Arch Linux integration system inspired by Windows Phone Link.

The project enables seamless, high-performance integration between an Android mobile device and an Arch Linux GNOME desktop over the local network without cloud servers, third-party storage, accounts, or mandatory internet connections.

---

> [!NOTE]
> **Project Status: Phase 1 (Foundation & Architecture)**
>
> Ferry is currently in **Phase 1**. The repository structure, core Linux service/UI skeletons, Android Jetpack Compose application skeleton, wire protocol specifications, and security models are established. Device discovery, pairing, and live file transfers are scheduled for subsequent phases.

---

## 🎯 Project Vision & Goals

* **Local-First & Private**: Direct peer-to-peer communication over local Wi-Fi. Zero telemetry, zero external servers.
* **Native Desktop Experience**: Deep integration with Arch Linux and GNOME Shell using GTK4, Libadwaita, and system daemons.
* **Modern Android Stack**: Native Kotlin application utilizing Jetpack Compose and Material 3 design tokens.
* **Strong Cryptographic Security**: Strict mutual authentication, out-of-band pairing, encrypted data transfers, and path traversal protections.

---

## 📦 MVP Scope

The initial Minimum Viable Product (MVP) focuses on **reliable bidirectional file transfers**:

1. **Local Device Discovery** (mDNS / DNS-SD)
2. **Secure Device Pairing** (Out-of-band SAS code confirmation)
3. **Persistent Trusted-Device Identity** (Cryptographic keys in SQLite / Android Keystore)
4. **Linux → Android File Transfer**
5. **Android → Linux File Transfer**
6. **Real-time Transfer Progress & Status**
7. **Transfer Cancellation & Graceful Failure Handling**
8. **Transfer Integrity Verification** (End-to-end SHA-256 validation)
9. **Transfer History**
10. **Native Linux (Libadwaita) and Android (Compose) UIs**

*Out of scope for initial MVP (Future milestones)*: SMS, calls, notification sync, clipboard sync, media controls, screen mirroring, remote camera/mic, and filesystem browsing.

---

## 🏛️ Architecture Overview

```
       ┌────────────────────────┐
       │   Android Application  │ (Kotlin + Jetpack Compose)
       └───────────┬────────────┘
                   │
                   │ Local Wi-Fi (Authenticated TLS / Noise)
                   │
       ┌───────────┴────────────┐
       │  Linux Ferry Service   │ (Python + Asyncio + SQLite)
       └───────────┬────────────┘
                   │
                   │ Local IPC (Async socket / D-Bus)
                   │
       ┌───────────┴────────────┐
       │ Linux GNOME Desktop UI │ (GTK4 + Libadwaita)
       └────────────────────────┘
```

The system strictly divides duties into:
* **Control Plane**: Device discovery, pairing handshakes, session negotiation, metadata exchange, and status updates.
* **Data Plane**: High-throughput, chunked streaming file transmission with inline integrity checks.

---

## 📂 Repository Structure

```
Ferry/
├── AGENTS.md                  # Operating manual for AI agents
├── README.md                  # Project overview and instructions
├── .gitignore                 # Repository ignore rules
├── docs/                      # Core architectural and protocol documentation
│   ├── ARCHITECTURE.md        # System boundaries, data flows, control vs data plane
│   ├── PROTOCOL.md            # Ferry v1 wire protocol specification
│   ├── SECURITY.md            # Threat model, cryptographic identity, safe file IO
│   ├── DEVELOPMENT.md         # Environment setup and developer workflows
│   ├── TESTING.md             # Testing strategy and test suites
│   ├── DECISIONS.md           # Architectural Decision Records (ADRs)
│   └── PROJECT_STATE.md       # Persistent context and current milestone status
├── linux/                     # Linux Service and GNOME Desktop Application
│   ├── pyproject.toml         # Python packaging configuration
│   ├── src/ferry_linux/       # Application source code
│   │   ├── core/              # Daemon, configuration, SQLite persistence
│   │   ├── protocol/          # Protocol models and serializers
│   │   └── ui/                # GTK4 / Libadwaita interface
│   └── tests/                 # Linux unit and integration tests
└── android/                   # Android Mobile Application
    ├── build.gradle.kts       # Gradle root build configuration
    ├── settings.gradle.kts    # Module settings and plugin repositories
    ├── gradle/libs.versions.toml # Dependency version catalog
    ├── gradlew                # Gradle wrapper script
    └── app/                   # Android application module (Kotlin + Compose)
        ├── src/main/          # App manifest, source code, UI, resources
        └── src/test/          # Android unit tests
```

---

## 🛠️ Prerequisites

* **Linux**: Arch Linux, GNOME 45+, Python 3.12+, `gtk4`, `libadwaita`, `python-gobject`.
* **Android**: Android 8.0+ (API 26+), Java 17+, Android SDK Platform 35+, `adb`.

---

## 🚀 Quick Start

### 1. Linux Application
```bash
# Run unit tests
python3 -m unittest discover -s linux/tests -v

# Launch the desktop UI
python3 -m ferry_linux

# Run the core service in headless daemon mode
python3 -m ferry_linux --service
```

### 2. Android Application
```bash
# Run unit tests
cd android && ./gradlew testDebugUnitTest

# Build debug APK
cd android && ./gradlew assembleDebug

# Install and launch on connected Android device via ADB
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n dev.ferry.app/.MainActivity
```

---

## 📖 Further Documentation

* [Operating Manual for AI Agents](file:///home/sanjeet/Projects/Ferry/AGENTS.md)
* [Current Project State & Status](file:///home/sanjeet/Projects/Ferry/docs/PROJECT_STATE.md)
* [System Architecture](file:///home/sanjeet/Projects/Ferry/docs/ARCHITECTURE.md)
* [Ferry Protocol Specification](file:///home/sanjeet/Projects/Ferry/docs/PROTOCOL.md)
* [Security & Threat Model](file:///home/sanjeet/Projects/Ferry/docs/SECURITY.md)
* [Development & Environment Guide](file:///home/sanjeet/Projects/Ferry/docs/DEVELOPMENT.md)
* [Testing Guide](file:///home/sanjeet/Projects/Ferry/docs/TESTING.md)
* [Architectural Decisions](file:///home/sanjeet/Projects/Ferry/docs/DECISIONS.md)
