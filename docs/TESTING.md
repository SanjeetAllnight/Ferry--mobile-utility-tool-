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
# Run all Linux tests (161 tests)
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
# Run Android local unit tests (57 tests)
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
* **Phase 3D (Transfer Reliability & Error Recovery)**:
  - 12 new reliability tests in `test_transfer.py` (class `TestPhase3DReliability`):
    - IO error handling in `begin()`, `receive_chunk()` (disk-full scenario)
    - `cancel()` safety from IDLE, ACCEPTED, COMPLETED states
    - `stream_chunks()` source-file disappearance raises `TransferError`
    - Integrity mismatch leaves no `.part` or final file
    - `_record_transfer_once()` idempotency (no duplicate DB rows)
    - `_cancel_incoming_transfer_for_peer()` cleans `.part` and fires callbacks
    - Stale `.part` cleanup on service `start()`
  - 5 new reliability tests in Android `TransferTest.kt` (tests 27–31):
    - `cancel()` from IDLE is safe
    - `receiveChunk()` after cancel throws `IllegalStateException`
    - Integrity mismatch leaves no `.part` or final file
    - `cancel()` after `finalise()` is safe
    - Hash failure removes `.part` file
  - **218 total tests: 161 Linux + 57 Android, all passing.**
  - See `docs/PHASE_3D_REPORT.md` for full reliability audit and change log.
* **Phase 3D.1 (Physical Reliability Acceptance Verification)**:
  - Physical tests PT-1 (disconnect mid-transfer), PT-2 (Cancel via UI), PT-3 (Recovery after failure) executed manually with real Android device and Arch Linux desktop.
  - All three tests PASSED: `.part` files correctly purged mid-flight, correct `FAILED`/`CANCELLED`/`COMPLETED` history entries recorded, Linux GUI did not freeze.
  - See `docs/PHASE_3D.1_PHYSICAL_VERIFICATION.md` for the full physical test log and file hash comparisons.
* **Phase 3E Task 1 (Persistent Interrupted Transfer Foundation)**:
  - 15 new Linux tests in `TestPhase3ETask1InterruptedTransfer` (tests E01–E15):
    - `interrupt()` from `TRANSFERRING` → `INTERRUPTED`
    - `.part` file retained on disk after `interrupt()`
    - `bytes_received` matches actual disk size
    - `resume_chunk_index` correct for full-chunk boundaries
    - File handle closed after `interrupt()`
    - `interrupt()` idempotent (second call no-ops)
    - Metadata accessible after `interrupt()`
    - `interrupt_info()` snapshot has all required keys
    - `interrupt()` on `COMPLETED`/`FAILED`/`CANCELLED` is a no-op
    - `interrupt_info()` returns `None` when not `INTERRUPTED`
    - Zero-byte transfer → `FAILED`, not `INTERRUPTED`
    - `TRANSFERRING→INTERRUPTED` is a valid state transition
    - `INTERRUPTED` has no outgoing transitions in Task 1
  - 7 new Linux tests in `TestPhase3ETask1DatabaseMigration` (tests DB01–DB07):
    - Schema v2 → v3 migration adds all 7 new columns
    - Existing rows survive migration intact
    - Migration is idempotent
    - Old rows have NULL for all new resume columns
    - `save_interrupted_transfer` + `get_interrupted_transfer` round-trip
    - `get_interrupted_transfer` returns None for COMPLETED transfers
    - `expire_interrupted_transfers()` marks expired rows as FAILED
  - **240 total tests: 183 Linux + 57 Android, all passing.**
  - See `docs/PHASE_3E_TASK1_REPORT.md` for full implementation log.
* **Phase 3E Task 2 (Resume Negotiation Protocol)**:
  - 11 new Linux protocol tests in `TestResumeProtocolModels` (`linux/tests/test_protocol_models.py`):
    - `TRANSFER_RESUME_REQUEST` serialization and envelope framing
    - `TRANSFER_RESUME_REQUEST` deserialization via `from_dict()`
    - Invalid transfer ID format rejected
    - Negative offset rejected
    - Negative chunk index rejected
    - Invalid SHA-256 (length, non-hex, uppercase) rejected
    - Invalid protocol version rejected
    - Offset / chunk mismatch rejected
    - `TRANSFER_RESUME_ACCEPT` serialization, deserialization, and validation
    - `TRANSFER_RESUME_REJECT` serialization, deserialization, and validation
    - Invalid reject reason rejected (all 6 valid enum reasons verified)
  - 10 new Linux transfer tests in `TestPhase3ETask2ResumeNegotiation` (`linux/tests/test_transfer.py`):
    - `prepare_resume_request()` on valid `.part` file
    - `prepare_resume_request()` uses actual on-disk size (authoritative)
    - `prepare_resume_request()` computes SHA-256 by streaming from disk (not from in-memory accumulator)
  - Missing `.part` file raises `FileNotFoundError`
    - Calling `prepare_resume_request()` in non-INTERRUPTED states raises `RuntimeError`
    - Partial-file boundary validation (unaligned bytes raise `ValueError`, no truncation or padding)
    - Large `.part` streaming without whole-file loading (bounded 64 KiB reads verified)
    - Zero-byte partial file on disk raises `ValueError`
    - Inconsistent DB metadata raises `ValueError`
    - Service layer message dispatch routes resume messages without unhandled errors
* **Session State Propagation Fix**:
  - Added `test_ui_state_propagation.py` with 2 tests verifying that `IPC_SESSION_UPDATE` carries required UI identity fields.
  - **263 total tests: 225 Linux + 57 Android, all passing.**
  - See `docs/SESSION_STATE_FIX_REPORT.md` for full bug fix report.

---

## 3. Pre-Commit / Pre-Release Verification Checklist

Every AI agent or developer must verify the following before concluding tasks:
1. `PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v` exits with code `0`.
2. `python3 -m compileall linux/src linux/tests` produces no syntax errors.
3. `cd android && ./gradlew testDebugUnitTest` exits with code `0`.
4. `cd android && ./gradlew assembleDebug` successfully produces a valid APK.
5. If physical device is connected, verify live ADB installation and logcat output.
