# Phase 3A & 3A.1 — Transfer Architecture & Secure Transport Verification Report

**Date:** 2026-08-30  
**Phase:** 3A (Transfer Transport Architecture) & 3A.1 (Physical Verification)  
**Status:** FULLY IMPLEMENTED & PHYSICALLY VERIFIED

---

## 1. Executive Summary

Phase 3A established the secure, high-performance file transfer transport for Ferry.
Phase 3A.1 physically verified end-to-end binary streaming over local Wi-Fi between the Linux host (`archnoir`) and the connected physical Android device (`Realme RMX3870`, Android 16 / SDK 36).

All automated unit test suites and physical transfer verifications passed with 100% integrity validation.

---

## 2. Architecture & Protocol Invariants

- **Transport Multiplexing (ADR 007)**: In-band multiplexing over the established ChaCha20-Poly1305 AEAD TCP connection.
- **Binary Chunk Framing (`TRANSFER_CHUNK`)**:
  ```text
  [4B "FYCH"] [16B UUID bytes] [4B seq uint32 BE] [4B payload_len uint32 BE] [N bytes raw chunk payload]
  ```
  - Header overhead: exactly 28 bytes per chunk.
  - Selected chunk payload size: 64 KiB (65,536 bytes).
- **Framing Discrimination**: Plaintext frames starting with magic `FYCH` bypass JSON parsing and are routed directly to the chunk receiver. Control messages (`TRANSFER_REQUEST`, `TRANSFER_ACCEPT`, `TRANSFER_COMPLETE`, `TRANSFER_RESULT`, `TRANSFER_CANCEL`, `TRANSFER_ERROR`) use the standard versioned `FerryEnvelope` JSON structure.
- **Security & Integrity**:
  - File transfers are only permitted on authenticated sessions in state `ESTABLISHED`.
  - Filenames supplied in metadata are strictly sanitised to basenames via `TransferMetadata.sanitiseFilename()` to prevent directory traversal attacks.
  - Chunks are incrementally hashed with SHA-256 while streaming to `<stagingDir>/<transfer_id>.part`.
  - Atomic rename to final destination occurs **only after** complete SHA-256 verification succeeds.
  - If verification fails or transfer is cancelled, the `.part` file is immediately deleted.

---

## 3. Physical Verification Results (Phase 3A.1)

Physical verification was executed across the local Wi-Fi subnet (`10.213.207.51` $\leftrightarrow$ `10.213.207.31`):

### Test 1: Small File Binary Transfer (131,072 bytes / 2 chunks)
- **Local File**: `small_doc.bin` (128 KiB)
- **Local SHA-256**: `497672ea5941a54728514582f3ef785c4bf4aa73dc165a39ba7b4d193d56b460`
- **Duration**: 0.155s (~825.8 KB/s)
- **Android Destination**: `/sdcard/Download/Ferry/staging/small_doc.bin`
- **ADB Inspected File Size**: 131,072 bytes
- **Remote SHA-256 (`adb shell sha256sum`)**: `497672ea5941a54728514582f3ef785c4bf4aa73dc165a39ba7b4d193d56b460`
- **Result**: **PASS** (100% hash match)

### Test 2: Multi-Megabyte Streaming Transfer (2,621,440 bytes / 40 chunks)
- **Local File**: `large_media.bin` (2.5 MiB)
- **Local SHA-256**: `18a9713e2ecb72f62b77de476dada47dac66c1b31feb09bea54438e1d43a4be6`
- **Duration**: 2.277s (Throughput: **1.10 MB/s**)
- **Android Destination**: `/sdcard/Download/Ferry/staging/large_media.bin`
- **ADB Inspected File Size**: 2,621,440 bytes
- **Remote SHA-256 (`adb shell sha256sum`)**: `18a9713e2ecb72f62b77de476dada47dac66c1b31feb09bea54438e1d43a4be6`
- **Result**: **PASS** (100% multi-chunk hash match)

### Test 3: Staging Isolation & Cleanliness
- **Check**: Examined Android destination for orphaned temporary files.
- **ADB Command**: `adb shell find /sdcard/Download/Ferry/staging/ -name "*.part"`
- **Result**: **PASS** (Zero `.part` files left behind)

---

## 4. Test Suite Summary

- **Linux Test Suite**: 127 tests passing (`PYTHONPATH=linux/src python3 -m unittest discover -s linux/tests -v`).
- **Android Test Suite**: 50 tests passing (`cd android && ./gradlew testDebugUnitTest`).
- **Android Debug Build**: APK builds and installs cleanly via ADB (`./gradlew assembleDebug`).

---

## 5. Phase 3A Conclusion

Phase 3A transport is **FULLY VERIFIED**. The underlying secure data channel is robust, performant, and ready for Phase 3B (Sender & Receiver UI).
