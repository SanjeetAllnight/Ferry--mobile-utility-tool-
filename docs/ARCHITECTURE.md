# Ferry System Architecture

This document describes the architectural design, component boundaries, process isolation, data flows, and dependency philosophy for **Ferry**.

---

## 1. System Overview

Ferry is a local-first, peer-to-peer device integration system connecting an Arch Linux GNOME desktop and an Android smartphone over the local network (LAN / Wi-Fi).

```
+-------------------------------------------------------------------------+
|                              Android Device                             |
|  +-------------------------------------------------------------------+  |
|  |                     Android UI (Jetpack Compose)                  |  |
|  +---------------------------------+---------------------------------+  |
|                                    | State / Intent                     |
|  +---------------------------------v---------------------------------+  |
|  |                     Android Ferry Core Engine                     |  |
|  |   - Discovery (NsdManager)      - Cryptographic Identity (KeyStore)|  |
|  |   - Netty / Coroutine Sockets   - Storage Access Framework (SAF)   |  |
|  +---------------------------------+---------------------------------+  |
+------------------------------------|------------------------------------+
                                     |
               Encrypted Local Wi-Fi | (TLS 1.3 / Noise Protocol)
                                     |
+------------------------------------v------------------------------------+
|                         Arch Linux GNOME Desktop                        |
|  +-------------------------------------------------------------------+  |
|  |                     Linux Ferry Service (Daemon)                  |  |
|  |   - Discovery (Avahi / Zeroconf)  - Crypto Identity (Ed25519)     |  |
|  |   - Asyncio TCP Engine            - SQLite Persistence            |  |
|  |   - Transfer Stream Pipeline      - Safe Local File System I/O    |  |
|  +---------------------------------+---------------------------------+  |
|                                    | Local IPC (UNIX Domain Socket)     |
|  +---------------------------------v---------------------------------+  |
|  |                     Linux Desktop UI (Libadwaita)                 |  |
|  |   - Adw.Application             - Device Status Cards             |  |
|  |   - Transfer Progress List      - GNOME Notifications             |  |
|  +-------------------------------------------------------------------+  |
+-------------------------------------------------------------------------+
```

---

## 2. Component Responsibilities

### 2.1. Android Application (`android/`)
* **Presentation**: Single-activity Jetpack Compose application styled with Material 3. Displays paired Linux hosts, discovery state, active transfer progress, and pairing approval dialogs.
* **Ferry Core (Android)**:
  * Manages local network discovery using Android Network Service Discovery (`NsdManager` / mDNS).
  * Manages persistent device keypairs inside Android Keystore (`AndroidKeyStore` provider).
  * Handles TCP transport, framing, and TLS/Noise encryption.
  * Interfaces with Android Storage Access Framework (SAF) and `MediaStore` for safe, sandbox-compliant file reading and writing.
  * Foreground Service lifecycle management for uninterrupted transfers.

### 2.2. Linux Ferry Service (`linux/src/ferry_linux/core/`)
* **Background Daemon**: Autonomous `asyncio` service running as a user systemd service or background process.
* **Responsibilities**:
  * Broadcasts and listens for Ferry mDNS services on the local subnet.
  * Maintains the persistent SQLite database (`~/.local/share/ferry/ferry.db`) for paired devices, trust tokens, transfer logs, and user preferences.
  * Manages incoming and outgoing authenticated network connections.
  * Implements safe file I/O: path sanitization, atomic writing (`.part` temporary files), SHA-256 integrity verification, and destination quarantine checks.
  * Exposes a lightweight local IPC interface (over UNIX Domain Socket) for UI clients.

### 2.3. Linux Desktop UI (`linux/src/ferry_linux/ui/`)
* **Native GNOME Application**: Built with Python, GTK4, and Libadwaita 1 (`Adw.Application`).
* **Responsibilities**:
  * Communicates with the background Ferry service via local IPC.
  * Displays discovered devices, paired devices, and active/completed transfers.
  * Handles user confirmations (e.g. accepting a new pairing PIN or accepting an incoming transfer request).
  * Triggers desktop notifications via FreeDesktop Notification specification.
  * **Strict Boundary**: The UI never directly handles network sockets, crypto handshakes, or raw file streaming. If the UI is closed, active background transfers continue in the service.

---

## 3. Separation of Planes: Control vs. Data Plane

Ferry strictly separates control and data operations to ensure responsiveness, safety, and scalability.

```
CONTROL PLANE                                DATA PLANE
=============                                ==========
[ Discovery & Announce ]                     [ Chunk Stream Pipeline ]
       │                                            │
[ Mutual Auth / TLS Handshake ]              [ Zero-Copy / Buffered I/O ]
       │                                            │
[ Transfer Request & Approval ]              [ Per-Chunk Checksum / MAC ]
       │                                            │
[ Pause / Resume / Cancel Signals ] ───────> [ Stream Termination ]
       │                                            │
[ Final Status & Verification ] <─────────── [ SHA-256 Digest Match ]
```

### Control Plane
* Transport: High-reliability TCP connection with length-prefixed JSON envelopes.
* Functions:
  * Device discovery metadata (`dev.ferry.v1`).
  * Authentication, cryptographic challenge-response, and SAS (Short Authentication String) exchange.
  * File transfer negotiation (metadata, file size, MIME type, checksum, user accept/reject).
  * Progress events, pause/resume signaling, cancellation, and error notifications.

### Data Plane
* Transport: Dedicated high-throughput TLS streaming connection.
* Functions:
  * Segmented binary streaming (standard 64 KiB or 128 KiB chunks).
  * Inline chunk sequencing and flow control.
  * Streaming SHA-256 calculation for end-to-end payload verification.
  * Direct disk staging via temporary files (`<filename>.<transfer_id>.part`).

---

## 4. Communication & IPC Boundaries

| Boundary | Mechanism | Security / Trust Policy |
| :--- | :--- | :--- |
| **Android ↔ Linux** | TCP over Wi-Fi (Port 53770 default) | Mutual TLS / Authenticated Key Exchange. Untrusted until paired. |
| **Linux Service ↔ Linux UI** | UNIX Domain Socket (`$XDG_RUNTIME_DIR/ferry.sock`) | Local user permissions (mode `0600`). Trusted. |
| **Service ↔ Filesystem** | Python `pathlib` + `os.open` (O_EXCL, strict paths) | Sandboxed to configured Downloads/Ferry directory. |
| **Android ↔ Filesystem** | Android SAF (`DocumentFile` / `ContentResolver`) | Scoped to user-granted storage trees or standard Download directory. |

---

## 5. Persistence Boundaries

All persistent state on Linux is stored in standard XDG locations:
* Configuration: `$XDG_CONFIG_HOME/ferry/config.json` (defaults to `~/.config/ferry/config.json`)
* Database: `$XDG_DATA_HOME/ferry/ferry.db` (defaults to `~/.local/share/ferry/ferry.db`)
* Runtime Sockets: `$XDG_RUNTIME_DIR/ferry.sock` (defaults to `/run/user/<uid>/ferry.sock`)

All persistent state on Android is stored in app-private storage:
* Cryptographic Keypairs: Android Keystore (`AndroidKeyStore`)
* App Preferences: Jetpack DataStore / SharedPreferences
* Transfer History: Local Room / SQLite database

---

## 6. Dependency & Architecture Philosophy

1. **No Cloud, No Accounts**: Ferry never contacts external servers for telemetry, account authentication, or relay.
2. **Minimal Third-Party Bloat**: Use platform-native tools and robust standard libraries (Python `asyncio`/`sqlite3`/`gi`, Android `kotlinx.coroutines`/`Jetpack`).
3. **Resilience**: Network dropouts or app closures must result in deterministic error states without data corruption.
4. **Extensibility**: The control plane message format is strictly versioned (`protocol_version: 1`), enabling future additions (clipboard, notifications) without breaking core transfer infrastructure.

---

## 7. Local Discovery Subsystem Architecture (Phase 2A)

```
+-------------------------------------------------------------------------------+
|                             mDNS / DNS-SD Subnet                              |
|                       Service Type: _ferry._tcp.local.                        |
+---------------------------------------+---------------------------------------+
                                        |
                 +----------------------+----------------------+
                 |                                             |
+----------------v----------------------+   +------------------v----------------+
|       Linux Discovery Manager         |   |      Android Discovery Engine     |
|   (zeroconf.asyncio.AsyncZeroconf)    |   |     (android.net.nsd.NsdManager)  |
|                                       |   |                                   |
| - Registers: AsyncServiceInfo         |   | - Registers: NsdServiceInfo       |
| - Browses: AsyncServiceBrowser        |   | - Browses: discoverServices()     |
| - Resolves: async_request()           |   | - Resolves: resolveService()      |
| - Deduplicates: device_id in memory   |   | - MulticastLock: WiFi management  |
| - Dispatches: UI callback events      |   | - StateFlow: DiscoveredDevice list|
+---------------------------------------+   +-----------------------------------+
```

### DiscoveredDevice vs. TrustedDevice Boundary
* **`DiscoveredDevice`**: Ephemeral in-memory object representing an unauthenticated peer visible on the LAN. Holds transport info (IPs, port, TXT metadata) and availability flags. Does **not** grant trust or allow file transfers.
* **`TrustedDevice`**: Cryptographically authenticated identity persisted in SQLite (Linux) or Keystore (Android) following explicit user pairing in Phase 2C. Discovered devices only become trusted after SAS out-of-band verification.
