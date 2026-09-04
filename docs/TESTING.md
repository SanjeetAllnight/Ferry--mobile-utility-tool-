# Ferry Testing Strategy & Test Suites

This document outlines the testing strategy, test layers, and verification commands for Ferry across Linux and Android.

---

## 1. Testing Pyramid & Strategy

```
              ┌────────────────────────┐
              │   End-to-End Tests     │  (Future: Full Wi-Fi transfer over ADB)
              ├────────────────────────┤
              │   Integration Tests    │  (IPC client/server, SQLite persistence)
              ├────────────────────────┤
              │ Protocol Framing Tests │  (Serialization, byte framing, validation)
              ├────────────────────────┤
              │      Unit Tests        │  (Core models, config, crypto, helpers)
              └────────────────────────┘
```

---

## 2. Test Suites by Component

### 2.1. Linux Tests (`linux/tests/`)
* **Framework**: Python standard library `unittest` (compatible with `pytest`).
* **Test Modules**:
  * `test_config.py`: Verifies XDG configuration directory resolution, default values, and JSON persistence.
  * `test_db.py`: Verifies SQLite schema creation, migrations, and CRUD operations for trusted devices and transfer history.
  * `test_protocol_models.py`: Verifies protocol v1 binary framing (`FY` magic bytes + uint32 length header), JSON envelope serialization, and Phase 3A transfer payload types.
  * `test_discovery_models.py`: Verifies TXT property decoding, IP address parsing, and protocol version compatibility.
  * `test_discovery_manager.py`: Verifies `DiscoveryManager` listener callbacks, service state transitions (Add/Update/Remove), and self-suppression.
  * `test_discovery_integration.py`: Live local mDNS announcement and discovery loopback test using `AsyncZeroconf`.
  * `test_identity.py`: Verifies Ed25519 identity key generation, file permissions (0600), signing, and verification.
  * `test_session.py`: Verifies session state machine, X25519 DH exchange, HKDF key derivation, 6-digit SAS code derivation, and ChaCha20-Poly1305 AEAD framing.
  * `test_control_plane.py`: Verifies end-to-end AKE, bidirectional interactive pairing flows, remote/local decision sequences, trust persistence, reconnection with pinned keys, and unpairing.
  * `test_transfer.py`: Verifies Phase 3A file transfer state machine, chunk framing (`FYCH`), filename sanitization, incremental SHA-256 validation, and atomic staging lifecycle.
  * `test_receiver.py`: Verifies Phase 3B Linux receiver event bus, interactive approval/rejection lifecycle, download directory resolution, and error handling.

```bash
# Run all Linux tests (149 tests)
PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v
```

### 2.2. Android Tests (`android/app/src/test/`)
* **Framework**: JUnit 4 / Kotlin Test Runner.
* **Test Modules**:
  * `ProtocolConstantsTest.kt`: Verifies protocol version constants, message types, and schema contracts matching Linux models.
  * `DiscoveredDeviceTest.kt`: Verifies TXT attribute parsing, host validation, protocol version checks, and null safety.
  * `SessionTest.kt`: Verifies session framing, AEAD encryption/decryption, and state transitions.
  * `TransferTest.kt`: Verifies transfer state machine, chunk calculation, 0-byte file support, and SHA-256 digest validation.

```bash
# Run Android local unit tests (52 tests)
cd android && ./gradlew testDebugUnitTest
```

### 2.3. Physical End-to-End Verification History
* **Phase 2A (Discovery)**: Bidirectional discovery over LAN mDNS verified.
* **Phase 2B/2C (Interactive Trust & AKE)**: Handshake, SAS pairing dialogs, and SQLite/AndroidKeyStore identity verified.
* **Phase 3A/3B (Transfer Transport & Linux Receiver)**: Binary `FYCH` chunk streaming over AEAD TCP verified with SHA-256 integrity and approval dialogs.
* **Phase 3C / 3C.1 (User-Facing File Transfer)**:
  - Linux `Gtk.FileDialog` and Android SAF document picker verified.
  - Bidirectional transfers (up to 16.5 MB at ~11.8 MB/s) with live UI progress indicators verified.
  - Incoming transfer approval, cancellation mid-flight, and transfer history verified on physical Realme device and Arch Linux desktop.
  - See `docs/PHASE_3C.1_PHYSICAL_VERIFICATION.md` for full physical test log and hash comparisons.

---

## 3. Pre-Commit / Pre-Release Verification Checklist

Every AI agent or developer must verify the following before concluding tasks:
1. `PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v` exits with code `0`.
2. `python3 -m compileall linux/src linux/tests` produces no syntax errors.
3. `cd android && ./gradlew testDebugUnitTest` exits with code `0`.
4. `cd android && ./gradlew assembleDebug` successfully produces a valid APK.
5. If physical device is connected, verify live ADB installation and logcat output.
