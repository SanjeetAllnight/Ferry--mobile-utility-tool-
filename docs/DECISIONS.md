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

---

## ADR 006: Local Device Discovery via mDNS / DNS-SD (AsyncZeroconf & NsdManager)

### Context
Ferry requires automatic peer discovery across local Wi-Fi / LAN without cloud relays, centralized servers, or manual IP entry.

### Decision
* Use DNS-SD / Multicast DNS (mDNS) with service type `_ferry._tcp.local.` (Linux) and `_ferry._tcp` (Android).
* **Linux**: Integrate `AsyncZeroconf` directly into the `FerryService` asyncio event loop for service registration, browsing, and resolution.
* **Android**: Use Android's native `NsdManager` with MulticastLock management for background/foreground battery safety.
* Discovery metadata in TXT records includes `v=1`, `id=<UUID>`, `name`, `type`, `os`, `port`, and `app_version`.
* Stable `id` (UUIDv4) is used for in-memory deduplication. Discovered IP addresses are treated purely as ephemeral transport endpoints and are never stored as permanent identity.

### Alternatives Considered
* *UDP Broadcast on custom port*: Subject to firewall blocks and lacks standard OS-level service naming and TXT metadata handling.
* *Bluetooth Low Energy (BLE) Discovery*: Higher battery drain, requires additional Bluetooth permissions/hardware, and unnecessary when Wi-Fi is already required for high-speed file transfers.

### Consequences
* Fast, native peer discovery across Arch Linux and Android on standard Wi-Fi subnets with zero external dependencies. Discovered devices remain unauthenticated until explicit pairing in Phase 2C.

---

## ADR 007 — In-Band Transfer Multiplexing (Phase 3A)

**Date:** 2026-08-30  
**Status:** Accepted

### Context

Phase 3A establishes the secure file transfer transport layer. A key architectural decision was required: should transfer data flow over the existing authenticated AEAD-encrypted control session, or should a separate authenticated data connection be established for bulk data?

### Two Options Considered

**Option A — In-band multiplexing over existing session:**  
All transfer control and data flows over the already-authenticated AEAD-encrypted TCP connection. Data chunks are binary frames starting with a 4-byte `FYCH` magic prefix (distinct from JSON control envelopes) inside the AEAD plaintext.

**Option B — Separate authenticated data connection:**  
A second TCP connection is established for bulk data, authenticated by a short-lived session token derived from the primary session key.

### Decision: Option A — In-band multiplexing

### Rationale

| Factor | Option A (chosen) | Option B |
|---|---|---|
| Implementation complexity | Low — reuse existing framing | High — 2nd auth handshake, token management |
| Android complexity | 1 socket per session | 2 sockets, lifecycle risk on Android |
| Security | Inherits AEAD session auth | Requires explicit token binding; wider attack surface |
| Protocol simplicity | Single connection | Significantly more complex |
| Concurrent transfers (future) | Transfer-ID multiplexing in frames | Separate sockets, but more state |
| Resumability (future) | Sequence numbers support it | Same |

For Ferry's MVP scope (single active transfer, LAN speeds, paired devices), Option A is simpler, safer, and sufficient. All TRANSFER_CHUNK frames are AEAD-authenticated by the session nonce counter, providing replay protection with no additional handshake.

### Chunk Frame Format (binary, inside AEAD plaintext)

```
[4B "FYCH"] [16B UUID bytes] [4B seq uint32 BE] [4B payload_len uint32 BE] [N bytes chunk data]
```

Total header: 28 bytes. Selected chunk payload size: 64 KiB.

### Consequences

- MVP: one active transfer at a time; busy-reject additional requests
- No second authentication mechanism required
- Future concurrent transfers can be added via transfer-ID without transport changes
- Transfer resumability (sequence numbers present) deferred to a later phase
- The data channel is as secure as the control channel; no new crypto is introduced

