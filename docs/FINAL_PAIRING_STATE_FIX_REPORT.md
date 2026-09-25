# Final Pairing State Fix Report

## Issue Summary
When performing fresh pairing, the devices successfully performed the cryptographic handshake and displayed the SAS codes. However, upon clicking "Accept" on the Linux daemon, the connection would immediately fail to transition to the `ESTABLISHED` state.
- **Android**: UI went blank and failed to show the trusted peer.
- **Linux**: Failed to show the Android device as connected or add it to the Trusted Devices list.

## Root Cause Analysis
The failure was not caused by a flaw in the cryptographic protocol or the pairing state machine logic on Linux, but rather by **two distinct bugs on the Android client**:

1. **AEAD Nonce Corruption (Concurrency Bug)**
   When the Android client transitioned to the `ESTABLISHED` state after receiving the pairing acceptance from Linux, it attempted to echo its own `PAIR_DECISION: ACCEPT` and immediately send its `CAPABILITIES` payload. 
   These two `sendEncrypted` calls were executed concurrently (one on the main coroutine, one in a launched `scope.launch` block). Because `sendEncrypted` uses a shared `sendSequence` variable to derive the ChaCha20-Poly1305 nonce, the concurrent access corrupted the nonce and interleaved the bytes on the socket.
   Linux received corrupted ciphertext, threw an exception during decryption, and immediately dropped the connection before it could save the peer to its trusted database.

2. **UI State Desynchronization**
   Android's `persistPendingTrust()` method successfully saved the new peer to the local database but failed to update the UI state. (Note: The UI bug is a secondary effect; the primary connection failure was due to the AEAD corruption).

## Resolution
1. **Serialized Encrypted Writes**: Consolidated the `PAIR_DECISION: ACCEPT` and `CAPABILITIES` payloads inside a single sequential `scope.launch` block in `FerryControlClient.kt`'s `acceptPairing()` method, ensuring strict sequential encryption and socket writes.
2. **Dead Code Removal**: Cleaned up unreachable blocks in Linux's `window.py::_update_trusted_devices` to prevent future confusion.

## Verification
- Android client successfully compiles and builds.
- Linux unit tests pass.
- Awaiting user to install the updated APK and perform the physical Phase 3C.2 verification test.
