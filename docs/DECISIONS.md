# Ferry Architectural Decision Records (ADRs)

This document records the foundational architectural decisions made for the **Ferry** project.

---

## ADR 001: Technology Stack Selection

### Context
Ferry requires a native desktop application for Arch Linux GNOME and a modern mobile application for Android devices with high performance and minimal runtime overhead.

### Decision
* **Linux**: Python 3 with `PyGObject` (GTK4 + Libadwaita 1) and `asyncio`.
* **Android**: Modern Kotlin with Jetpack Compose and Material 3 design system.

### Alternatives Considered
* *Electron / Web App*: Discarded due to heavy memory footprint, poor native GNOME integration, and bloated binary sizes.
* *Rust / C++ for Linux UI*: Highly performant, but GTK4/Libadwaita bindings in Python offer rapid iteration and seamless system integration on Arch Linux without long compilation cycles.
* *Flutter / React Native for Android*: Discarded in favor of Android-native Kotlin and Jetpack Compose to ensure native battery management, Storage Access Framework (SAF) integration, and Foreground Service reliability.

### Consequences
* Linux UI integrates natively with GNOME design language, dark mode, and system fonts.
* Android UI leverages modern declarative Compose APIs with minimum APK size and optimal power efficiency.

---

## ADR 002: Process Separation between Linux Service Daemon and Desktop UI

### Context
File transfers can be large and take time. If the desktop UI window is closed by the user, active transfers should continue uninterrupted.

### Decision
Architect the Linux component as two distinct layers:
1. **Ferry Service (Core Daemon)**: Autonomous background service owning networking, device discovery, TLS sessions, SQLite persistence, and file streaming.
2. **Ferry UI**: Lightweight GTK4/Libadwaita application communicating with the local service via a local UNIX domain socket IPC.

### Alternatives Considered
* *Monolithic UI + Networking*: Simpler to build initially, but closing the UI would terminate transfers, and background auto-discovery would not be possible without an open window.
* *D-Bus System Service*: More complex setup requiring root polkit policies. A user-session daemon with UNIX domain socket is simpler, safer, and fully unprivileged.

### Consequences
* Clear separation of concerns; UI crashes or restarts do not drop network connections or abort active file transfers.

---

## ADR 003: Versioned Binary-Framed JSON Protocol (`dev.ferry.v1`)

### Context
Ferry requires structured messaging for discovery, authentication, transfer negotiation, progress tracking, and error reporting.

### Decision
Implement a 2-byte magic prefix (`FY` / `0x46 0x59`) + 4-byte big-endian uint32 payload length, followed by a UTF-8 JSON message envelope (`protocol_version`, `message_id`, `type`, `payload`).

### Alternatives Considered
* *gRPC / Protocol Buffers*: Strong typing, but introduces compilation dependencies and schema generation complexity on both platforms.
* *Raw JSON lines over TCP*: Prone to streaming fragmentation issues and lacks explicit framing integrity.
* *WebSocket*: Adds HTTP upgrade overhead without tangible benefit over direct TCP sockets.

### Consequences
* Simple, human-readable message payloads with robust binary framing preventing buffer overruns and fragmentation bugs.

---

## ADR 004: Zero-Trust Local Security with Ed25519 and Out-of-Band SAS Pairing

### Context
Local Wi-Fi networks cannot be assumed secure. Rogue actors on the same LAN must not be able to impersonate devices or intercept transfers.

### Decision
* Every device generates an Ed25519 identity keypair.
* Pairing uses an out-of-band Short Authentication String (SAS) numeric PIN displayed on both screens.
* Network traffic is authenticated and encrypted via TLS 1.3 / ChaCha20-Poly1305.
* IP addresses, hostnames, and display names are treated purely as ephemeral hints, never as cryptographic identities.

### Alternatives Considered
* *Pre-shared password*: Poor user experience for pairing mobile devices with desktop.
* *Implicit LAN trust (no auth)*: Highly dangerous and vulnerable to malicious peers on open Wi-Fi.

### Consequences
* Robust security posture that guarantees end-to-end privacy and MITM resistance across untrusted networks.

---

## ADR 005: SQLite Persistence on Linux & Android Keystore on Mobile

### Context
Both ends need persistent storage for trusted peer identities, transfer history, and configuration.

### Decision
* **Linux**: Standard SQLite database (`ferry.db`) located in `$XDG_DATA_HOME/ferry/`.
* **Android**: Hardware-backed Android Keystore for private keys, Jetpack DataStore / Room for metadata.

### Alternatives Considered
* *Plain JSON files for database*: Inefficient for append-heavy transfer history and lacks transactional ACID guarantees.

### Consequences
* Reliable, transactional storage that survives application restarts and power loss.
