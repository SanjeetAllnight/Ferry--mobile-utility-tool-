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
  * `test_protocol_models.py`: Verifies protocol v1 binary framing (`FY` magic bytes + uint32 length header) and JSON envelope serialization/deserialization.

```bash
# Run all Linux tests
python3 -m unittest discover -s linux/tests -v
```

### 2.2. Android Tests (`android/app/src/test/`)
* **Framework**: JUnit 4 / Kotlin Test Runner.
* **Test Modules**:
  * `ProtocolConstantsTest.kt`: Verifies protocol version constants, message types, and schema contracts matching Linux models.

```bash
# Run Android local unit tests
cd android && ./gradlew testDebugUnitTest
```

### 2.3. Future Integration & End-to-End Testing (Phase 2+)
* **IPC Loopback Test**: Launch headless Ferry service, connect mock UI client over UNIX domain socket, verify event delivery.
* **Local Loopback Transfer Test**: Launch mock sender and receiver on localhost, transmit file, verify SHA-256 digest match.
* **ADB Device E2E Test**: Scripted test initiating transfer between Arch host and physical Android device via ADB reverse/Wi-Fi.

---

## 3. Pre-Commit / Pre-Release Verification Checklist

Every AI agent or developer must verify the following before concluding tasks:
1. `python3 -m unittest discover -s linux/tests -v` exits with code `0`.
2. `python3 -m compileall linux/src linux/tests` produces no syntax errors.
3. `cd android && ./gradlew testDebugUnitTest` exits with code `0`.
4. `cd android && ./gradlew assembleDebug` successfully produces a valid APK.
