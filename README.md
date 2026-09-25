# Raven

> Minimal, private Android ↔ Linux communication over your local network.

Raven is an ultra-minimal, privacy-focused utility for connecting your Android phone and Linux desktop over your local Wi-Fi or LAN. It handles fast, encrypted bidirectional file transfers and mirrors Android notifications directly to your desktop—with zero cloud servers, zero third-party relays, zero telemetry, and zero mandatory accounts.

Raven is **not** a Phone Link or KDE Connect replacement. It intentionally avoids remote control, SMS, call answering, screen mirroring, media controls, clipboard sync, and cloud synchronization. Its sole purpose is private local connectivity: moving files and mirroring notifications.

---

## Features

- **Local Network Discovery**: Automatically discovers peers on the local Wi-Fi / LAN using mDNS / DNS-SD (`_ferry._tcp`) without manual IP configuration.
- **Secure Device Pairing**: Interactive pairing flow with out-of-band 6-digit Short Authentication String (SAS) numeric verification.
- **End-to-End Encrypted Sessions**: Authenticated key exchange using Ed25519 identity keys, ephemeral X25519 Diffie-Hellman, HKDF-SHA256, and ChaCha20-Poly1305 AEAD framing.
- **Bidirectional File Transfer**: Send files from Android to Linux and Linux to Android.
- **Multiple Files & Folder Transfers**: Send multi-file batches and complete directory structures with recursive traversal and preserved folder hierarchy.
- **Large-File Streaming**: Memory-efficient chunked streaming (64 KiB binary frames) with inline SHA-256 integrity verification.
- **Transfer Progress & Cancellation**: Live transfer progress indicators and user-initiated transfer cancellation with automatic cleanup of partial (`.part`) files.
- **Transfer Recovery / Resumability**: Receiver-initiated resumption for interrupted transfers (currently verified with Android as receiver).
- **Android Share Sheet Integration**: Native `ACTION_SEND` and `ACTION_SEND_MULTIPLE` support to share single or multiple files directly from any Android app.
- **Android → Linux Notification Mirroring**: Proxies mobile notifications to the desktop via the standard FreeDesktop `org.freedesktop.Notifications` D-Bus interface.

---

## How It Works

```text
Android Device (Kotlin + Jetpack Compose)
   │
   │  Local Wi-Fi / LAN
   ▼
Raven Secure Channel (dev.ferry.v1)
   │
   ├── Local mDNS Discovery (_ferry._tcp)
   ├── Authenticated Key Exchange (Ed25519 + X25519)
   ├── Encrypted Session (ChaCha20-Poly1305 AEAD)
   ├── In-Band File Streaming (FYCH binary chunks)
   └── Notification Mirroring (notify.v1 frames)
   │
   ▼
Linux Desktop (Python 3 + GTK4 / Libadwaita)
```

Normal operation is strictly peer-to-peer over your local network. No packets leave your local network, and no cloud servers or relay proxies are involved.

---

## Security & Privacy

- **Local-First & Zero-Trust Network**: Being on the same Wi-Fi network does not grant trust. Local networks are treated as untrusted transport mediums.
- **Cryptographic Device Identity**: Every device generates a persistent Ed25519 keypair for mutual authentication. IP addresses and hostnames are treated only as ephemeral routing hints.
- **Out-of-Band Pairing**: Devices establish trust through an interactive 6-digit Short Authentication String (SAS) verification PIN displayed simultaneously on both screens.
- **Forward Secrecy & Encrypted Sessions**: Each session negotiates fresh ephemeral X25519 keys; session keys are derived using HKDF-SHA256 and authenticated with ChaCha20-Poly1305 AEAD.
- **File Integrity & Path Traversal Mitigations**: Incoming files undergo strict basename sanitization and path canonicalization. Data streams write to staged `.part` files and are atomically renamed only after full SHA-256 integrity verification.
- **Privacy-Preserving Notifications**: Mirrored notifications travel solely over the encrypted local link to the paired Linux machine. Notification content is display-only, is never written to disk by Raven, and is omitted from application logs.
- **Identity Key Storage**:
  - **Linux**: Private keys and trusted peer records are stored in an SQLite database (`$XDG_DATA_HOME/ferry/ferry.db`) with `0600` file permissions.
  - **Android**: Asymmetric keys are generated in and protected by hardware-backed `AndroidKeyStore` where supported. On certain devices or OEM skins (e.g., ColorOS) where hardware Keystore implementations exhibit known Ed25519 key generation defects, Raven safely falls back to software-backed key generation stored in private application storage (`Context.MODE_PRIVATE`).

---

## Supported Platforms

| Platform | Environment | Requirements |
| :--- | :--- | :--- |
| **Linux** | Arch Linux & modern Linux distributions (tested on Arch Linux x86_64) | GNOME 45+ / 50+ (Wayland / X11), Python 3.12+, GTK4, Libadwaita 1, systemd user session |
| **Android** | Android 8.0+ (Oreo, API 26+) through Android 16 (API 36) | Target SDK 35 (Android 15), ARM64 / ARMv7 / x86_64 |

### Required Android Permissions
- `INTERNET`, `ACCESS_NETWORK_STATE`, `ACCESS_WIFI_STATE`, `CHANGE_WIFI_MULTICAST_STATE` (Local discovery and socket transport)
- `POST_NOTIFICATIONS` (Android 13+ status notifications)
- `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC` (Keeps active file transfers alive when backgrounded)
- `BIND_NOTIFICATION_LISTENER_SERVICE` (Optional system access granted by the user to mirror notifications)

---

## Requirements

### Linux
- **Python**: Version 3.12 or newer (tested on Python 3.14)
- **Desktop Libraries**: `python-gobject` (PyGObject 3.46+), `gtk4`, `libadwaita` (1.0+)
- **Python Libraries**: `cryptography`, `zeroconf`
- **System Tools (optional, for desktop integration)**: `update-desktop-database`, `gtk-update-icon-cache`

### Android
- **Build Toolchain**: JDK 17+ (tested with OpenJDK 17, 21, and 26), Android SDK Platform 35+, Android Build-Tools 35+
- **Gradle Wrapper**: Included (`./gradlew`, Gradle 9.4.1)
- **Deployment**: Android device or emulator with Developer Options enabled; `adb` (Android Debug Bridge) for command-line installation

---

## Installation

### Linux Installation

1. **Clone the repository and enter the directory**:
   ```bash
   git clone https://github.com/user/Ferry.git raven
   cd raven
   ```

2. **Install system dependencies**:
   - **Arch Linux**:
     ```bash
     sudo pacman -S python python-gobject gtk4 libadwaita python-cryptography python-zeroconf
     ```
   - **Debian / Ubuntu 24.04+**:
     ```bash
     sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-cryptography python3-zeroconf
     ```

3. **Install the Python package**:
   You can install Raven in editable development mode:
   ```bash
   pip install -e linux/ --break-system-packages
   ```
   *(Alternatively, run directly from source checkout using `PYTHONPATH=linux/src` without installing).*

4. **Install desktop integration (systemd service, desktop launcher, icon)**:
   Raven includes an automated installer script:
   ```bash
   ./linux/install_integration.sh
   ```
   This installs:
   - User systemd service: `~/.config/systemd/user/ferry.service`
   - Desktop launcher entry: `~/.local/share/applications/dev.ferry.Ferry.desktop`
   - Application icon: `~/.local/share/icons/hicolor/256x256/apps/dev.ferry.Ferry.png`

5. **(Optional) Install Nautilus file manager right-click integration**:
   ```bash
   mkdir -p ~/.local/share/nautilus/scripts
   cp linux/scripts/nautilus/send_via_ferry.sh ~/.local/share/nautilus/scripts/"Send via Raven"
   chmod +x ~/.local/share/nautilus/scripts/"Send via Raven"
   ```

---

### Android Installation

Raven is built locally from source using Gradle. Prebuilt release binaries are not currently hosted in the repository.

1. **Build the debug APK**:
   ```bash
   cd android
   ./gradlew assembleDebug
   ```
   The built APK is output to:
   ```text
   android/app/build/outputs/apk/debug/app-debug.apk
   ```

2. **Install to a connected Android device via ADB**:
   ```bash
   adb install -r android/app/build/outputs/apk/debug/app-debug.apk
   ```
   *(Or transfer `app-debug.apk` to your phone and install it using an Android file manager).*

---

## Running Raven

### Linux

Raven consists of a headless background service daemon and a GTK4 / Libadwaita user interface.

- **Launch the desktop UI**:
  ```bash
  ferry
  ```
  *(If running from source without installation: `PYTHONPATH=linux/src python3 -m ferry_linux`)*.
  
  When the UI launches, it connects to the daemon via a local UNIX domain socket (`/run/user/<UID>/ferry/ferry.sock`). If the daemon is not running, the UI automatically starts it.

- **Run or manage the background daemon**:
  If you ran `install_integration.sh`, the service can be managed via `systemctl`:
  ```bash
  # Start the background daemon
  systemctl --user start ferry

  # Enable the daemon to start automatically on login
  systemctl --user enable ferry

  # Check daemon status
  systemctl --user status ferry
  ```
  Or run the daemon directly in a terminal:
  ```bash
  PYTHONPATH=linux/src python3 -m ferry_linux --service
  ```

### Android

1. Open the **Raven** app from your home screen or app drawer.
2. Grant required notification permissions when prompted (on Android 13+).
3. If using notification mirroring, enable the setting in the app and grant **Notification Access** in Android System Settings.

---

## First-Time Setup

Connecting an Android device to a Linux desktop requires a one-time cryptographic pairing:

1. **Connect**: Ensure both the Android device and Linux desktop are connected to the same local Wi-Fi or subnet.
2. **Start Raven**: Open Raven on Linux and on Android.
3. **Discover**: In the Android app, discovered Linux machines appear under "Discovered Devices".
4. **Initiate Pairing**: Tap **Pair** next to the discovered Linux device.
5. **Verify SAS PIN**: A pairing prompt will appear on both devices showing a 6-digit numeric Short Authentication String (SAS) code (e.g., `482 910`).
6. **Approve**: Verify that the numbers on both screens match exactly, then tap **Accept** on both devices.
7. **Trusted Connection**: Once approved, both devices persist the peer's public key.

> **New Device vs. Trusted Device**:
> - **New Device**: Requires manual pairing and SAS code confirmation.
> - **Trusted Device**: Reconnects automatically and transparently over the authenticated channel whenever both devices are active on the same network.

---

## Sending Files

### Android → Linux
- **Within Raven**: Tap the Floating Action Button (FAB) or "Send Files", select files with the system file picker, and confirm.
- **Via Android Share Sheet**: Select any file, photo, or group of items in any Android app (Gallery, Files, Browser), tap **Share**, choose **Raven**, and pick your desktop.

### Linux → Android
- **Header Bar**: Click **Send Files** or **Send Folder** in the Raven header bar.
- **Drag & Drop**: Drag files or folders directly from your file manager and drop them onto the Raven desktop window.
- **Nautilus Integration**: Right-click any file in GNOME Files (Nautilus) → **Scripts** → **Send via Raven**.
- **Command Line**: Pass files directly via CLI:
  ```bash
  ferry --send /path/to/file.ext
  ```

---

## Multiple Files & Folders

- **Batch Transmission**: When sending multiple files, Raven negotiates a batch session, provides progress for the entire set, and streams files sequentially.
- **Folder Preservation**: When a folder is sent, Raven recursively traverses all directories and reconstructs the relative subfolder hierarchy in the destination folder (`~/Downloads/Ferry` on Linux, `Downloads/Ferry` on Android).
- **Progress Tracking**: The transfer UI displays active batch progress (e.g., `Sending batch 2/5: document.pdf (64%)`) alongside transfer speed and remaining bytes.

---

## Notifications

Raven provides unidirectional, local-network notification mirroring from Android to Linux:

- **Desktop Integration**: Incoming mobile alerts appear through your desktop's native notification daemon (`org.freedesktop.Notifications`).
- **Display-Only**: Mirrored notifications are strictly informative. They do not support remote inline replies, remote dismissal back to the phone, or call control.
- **Local & Ephemeral**: Notification content is never routed through external servers, is never saved to disk on Linux, and is excluded from logs.
- **Spam & Loop Protection**: Built-in token-bucket rate limiting (10-event burst, 1/sec sustained) and SHA-256 content deduplication prevent notification flooding.
- **Intelligent Filtering**: Persistent notifications (music players, download bars), group summaries, non-clearable alerts, and Raven's own transfer notices are automatically filtered out.

### Setting Up Notification Mirroring
1. In the Raven Android app, tap the **Settings** (gear) icon.
2. Toggle **Notification Mirroring** ON.
3. When prompted, grant Raven **Notification Access** (Device & app notifications) in Android System Settings.
4. *OEM Note*: Devices with aggressive background task killers (such as ColorOS, MIUI, or OneUI) may terminate the notification listener service when the phone is locked. To ensure reliable mirroring, set Raven's battery usage to **Unrestricted** in Android's App Info settings.

---

## Configuration

### Linux Configuration
Configuration is saved in JSON format at:
```text
~/.config/ferry/config.json
```
*(or `$XDG_CONFIG_HOME/ferry/config.json`)*

Key settings:
- `device_name`: Display name advertised on mDNS (default: `<hostname> (Raven)`).
- `listen_port`: TCP control plane listening port (default: `53770`).
- `download_dir`: Destination directory for received files (default: `~/Downloads/Ferry`).
- `auto_accept_paired`: Automatically accept transfers from trusted peers without a prompt (default: `false`).
- `max_chunk_size`: Streaming chunk payload size in bytes (default: `65536` / 64 KiB).

### Android Configuration
Accessible from the gear icon in the Android app top bar:
- Device display name
- Notification Mirroring toggle
- Trusted paired devices management

---

## Background Operation

### Linux
The core Linux engine is designed as an independent service (`ferry --service`). When installed as a systemd user unit:
- The daemon starts silently upon desktop login.
- It maintains mDNS discovery and listens for transfers in the background.
- Files can be received without opening the GTK window (desktop notifications alert you to incoming requests).
- Opening the Raven GUI attaches to the existing background daemon; closing the GUI does not disconnect peers or cancel transfers.

### Android
- **Active Transfers**: When a file transfer starts, Raven promotes itself to an Android Foreground Service (`FerryTransferService`) with an active notification, ensuring transfers continue uninterrupted if you switch apps or lock the screen.
- **Notification Mirroring**: Handled by Android's system `NotificationListenerService`.
- Full 24/7 background socket persistence on Android is subject to standard OS Doze and OEM battery optimization policies.

---

## Troubleshooting

### Devices do not appear in discovery list
- Ensure both devices are connected to the same Wi-Fi network and subnet.
- Check router settings: ensure **AP Isolation** or **Client Isolation** is disabled (this feature blocks local peer-to-peer traffic).
- Verify Linux firewall rules permit TCP port `53770` and UDP port `5353` (mDNS):
  ```bash
  sudo ufw allow 53770/tcp
  sudo ufw allow 5353/udp
  ```
- Ensure an mDNS responder is active on Linux (e.g. `systemctl status avahi-daemon` or `systemd-resolved`).

### Pairing fails or hangs
- Check that the 6-digit SAS code displayed on Linux matches the code on Android.
- If a device was previously paired and re-installed, remove stale identity records:
  - **Linux**: Delete the trusted device record in `~/.local/share/ferry/ferry.db` or clear the database.
  - **Android**: Clear the paired device in Raven Settings or clear the app's data.

### File transfer fails
- Check disk space and verify write permissions for `~/Downloads/Ferry`.
- Ensure Wi-Fi connection remained stable during transmission.
- *Resumability*: If an interrupted transfer fails to resume on Linux, cancel and initiate a fresh transfer.

### Notifications do not mirror to Linux
- Verify **Notification Access** is granted in Android System Settings.
- Verify **Notification Mirroring** switch is toggled ON in Raven Android settings.
- Ensure OEM battery optimization is set to **Unrestricted** for the Raven app.
- Check that your Linux desktop environment has an active notification daemon implementing `org.freedesktop.Notifications` (e.g. GNOME Shell, Dunst, Mako).

### Linux GUI cannot connect to daemon
- Check if the daemon is running:
  ```bash
  systemctl --user status ferry
  ```
- Check if the IPC socket exists at `/run/user/<UID>/ferry/ferry.sock`.
- Try starting the daemon manually: `ferry --service`.

---

## Known Limitations

- **Sequential Transfers**: Raven processes one transfer operation at a time per peer session. Multi-file batch items are transmitted sequentially.
- **Linux Receiver Resume Path**: Transfer resumption is verified when Android is the receiving device. Resuming an interrupted transfer with Linux as the receiving device currently has a known limitation and is deferred to a future update (standard transfers work normally).
- **Unidirectional Notifications**: Notification mirroring is strictly Android → Linux and display-only (no remote dismissal, action buttons, or reply capabilities).
- **Read-Only Settings Dialog**: The Linux GUI settings dialog currently displays configuration values; modifying settings requires editing `~/.config/ferry/config.json`.
- **Android KeyStore OEM Fallback**: On specific Android OEM devices where the hardware `AndroidKeyStore` produces invalid Ed25519 keys, Raven automatically falls back to software-backed key storage in private app data.

---

## Development

### Repository Overview
- `linux/`: Python GTK4 / Libadwaita desktop application, core asyncio daemon, SQLite persistence, and unit tests.
- `android/`: Native Kotlin application using Jetpack Compose, Material 3, NsdManager, and JUnit tests.
- `docs/`: Technical specifications, security analysis, wire protocols, and Architectural Decision Records (ADRs).

### Running Tests

**Linux Unit Tests**:
```bash
PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v
```

**Android Unit Tests**:
```bash
cd android && ./gradlew testDebugUnitTest
```

### Deeper Documentation
For in-depth architectural and protocol documentation, see:
- [System Architecture](docs/ARCHITECTURE.md)
- [Wire Protocol Specification](docs/PROTOCOL.md)
- [Security Model & Cryptography](docs/SECURITY.md)
- [Developer Setup & Workflows](docs/DEVELOPMENT.md)
- [Testing Strategy & Test Suites](docs/TESTING.md)
- [Architectural Decision Records (ADRs)](docs/DECISIONS.md)
- [Persistent Project State](docs/PROJECT_STATE.md)
- [Agent Operating Manual](AGENTS.md)

---

## Architecture Summary

Raven separates concerns into an autonomous background daemon and a presentation layer:

```text
┌─────────────────────────────────┐       ┌─────────────────────────────────┐
│       Android Application       │       │        Linux Desktop UI         │
│  (Kotlin + Jetpack Compose)     │       │       (GTK4 + Libadwaita)       │
└────────────────┬────────────────┘       └────────────────┬────────────────┘
                 │                                         │
                 │ Local Wi-Fi (dev.ferry.v1)              │ Local UNIX Socket IPC
                 │                                         │ (/run/user/UID/ferry.sock)
                 ▼                                         ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                           Linux Core Service                              │
│         (Python asyncio + Zeroconf mDNS + SQLite + Cryptography)          │
└───────────────────────────────────────────────────────────────────────────┘
```

- **Control Plane**: Binary framing prefix (`0x46 0x59` / `FY`) followed by a 4-byte payload length and JSON message envelopes (`HANDSHAKE_INIT`, `PAIR_REQUEST`, `TRANSFER_REQUEST`, `BATCH_REQUEST`, `NOTIFICATION_POST`).
- **Data Plane**: Binary `FYCH` chunk frames multiplexed in-band over the authenticated ChaCha20-Poly1305 session (64 KiB chunks with sequence numbering and incremental SHA-256 calculation).
- **Internal Identifiers**: Internal components retain stable protocol identifiers (`dev.ferry.v1`, package `dev.ferry.app`, discovery service `_ferry._tcp.local.`) to maintain backward compatibility.

---

## Project Structure

```text
Raven/
├── AGENTS.md                    # Agent operational manual and rules
├── README.md                    # Project documentation and user guide
├── android/                     # Android Jetpack Compose mobile app
│   ├── app/src/main/kotlin/     # Kotlin source code (discovery, net, security, UI)
│   ├── app/build.gradle.kts     # Android module build configuration
│   └── gradlew                  # Gradle build wrapper
├── linux/                       # Linux service and GTK4 desktop application
│   ├── desktop/                 # .desktop launcher and 256x256 icon
│   ├── install_integration.sh   # Desktop and systemd integration script
│   ├── pyproject.toml           # Python packaging definition
│   ├── scripts/nautilus/        # GNOME Files (Nautilus) send script
│   ├── src/ferry_linux/         # Application source (core daemon, protocol, UI)
│   ├── systemd/                 # User systemd service definition
│   └── tests/                   # Python unittest test suites
└── docs/                        # Architectural and technical specifications
    ├── ARCHITECTURE.md          # Detailed system architecture
    ├── DECISIONS.md             # Architectural Decision Records (ADRs)
    ├── DEVELOPMENT.md           # Developer environment reference
    ├── PROJECT_STATE.md         # Current factual status and test results
    ├── PROTOCOL.md              # dev.ferry.v1 wire protocol specification
    └── SECURITY.md              # Threat model and cryptographic specification
```

---

## Scope & Philosophy

**Current Scope**:
- High-performance local file transfer (single files, batches, recursive folders)
- Android → Linux notification mirroring

**Explicitly Excluded / Deferred Scope**:
- SMS messaging and call control
- Screen mirroring and remote desktop
- Remote filesystem browsing
- Media controls
- Shared clipboard synchronization
- Cloud relay or external server synchronization
- Remote phone control

Raven is intentionally minimal: connect your phone to your PC locally, move files securely, and mirror useful notifications. Nothing more.

---

## License

This repository does not currently contain an explicit open-source license. All rights are reserved by the original authors.

---

## Acknowledgements

Raven is built upon and inspired by excellent open-source libraries:
- [PyGObject](https://pygobject.gnome.org/) and [GNOME Libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/)
- [Cryptography (hazmat primitives)](https://cryptography.io/)
- [AsyncZeroconf](https://github.com/python-zeroconf/python-zeroconf)
- [Jetpack Compose](https://developer.android.com/jetpack/compose) and [Material 3](https://m3.material.io/)
