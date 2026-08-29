# Phase 2C.1 Physical Verification Report

**Status:** Verified (Complete)
**Date:** 2026-08-30

## Overview
Phase 2C implemented the Interactive Trust, Pairing UX & Android Identity Storage for the Ferry protocol. This report documents the final physical verification using a real Android 14 device (Realme RMX3870) and a Linux desktop, confirming the successful operation of the complete authenticated session lifecycle.

## Verification Scenarios and Results

1.  **Initial Discovery & Handshake:**
    *   **Result:** Pass. Devices successfully advertise via mDNS, discover each other, and exchange cryptographic primitives.
2.  **Pairing UI and SAS Match:**
    *   **Result:** Pass. The 6-digit Short Authentication String (SAS) was successfully displayed on both Linux (`physical_test_runner.py`) and the Android Compose UI.
3.  **Connection Acceptance:**
    *   **Result:** Pass. Tapping the "Accept" button successfully transitioned the session from `PAIRING` to `ESTABLISHED` without `NetworkOnMainThreadException` crashes.
4.  **Persistent Trust & Reconnection:**
    *   **Result:** Pass. After an initial successful pairing, tapping "Disconnect" followed by "Connect" immediately resumed an `ESTABLISHED` (`● Secure`) connection, bypassing the SAS prompt by retrieving the verified public keys from the persistent `FerryTrustStore` on both platforms.

## OEM Bugs and Workarounds Discovered During Testing

During physical verification, two critical real-world device issues were discovered and addressed:

1.  **AndroidKeyStore Hardware Ed25519 Implementation Bug:**
    *   **Issue:** On the Realme RMX3870 (Android 14), attempting to generate a hardware-backed `Ed25519` key via `AndroidKeyStore` results in the system silently ignoring the `Ed25519` parameter and generating a standard `secp256r1` ECDSA keypair. This caused a `java.lang.IllegalArgumentException: private key algorithm does not match algorithm of public key in end entity certificate` upon KeyStore load, and signature verification failures across the network due to invalid key structures (91 bytes vs the expected 44 bytes).
    *   **Resolution:** Implemented a robust fallback mechanism in `FerryIdentity.kt`. The system attempts hardware-backed generation, validates the resulting public key size (must be exactly 44 bytes for a valid X.509 Ed25519 key). If it detects the OEM bug, it securely deletes the corrupted hardware key, gracefully falls back to the software `Ed25519` provider (Conscrypt), and safely stores the key material using Android's `EncryptedSharedPreferences`.
2.  **Android UI Thread Network I/O Crash:**
    *   **Issue:** The initial implementation invoked `FerryControlClient.sendEncrypted()` synchronously from the Compose `onClick` handlers, causing `NetworkOnMainThreadException` and abruptly dropping the TCP connection mid-handshake when a user clicked Accept/Reject.
    *   **Resolution:** Wrapped the pairing decision network transmissions inside the `FerryControlClient`'s existing background `CoroutineScope` using `Dispatchers.IO`.

## Conclusion
The cryptographic control plane and interactive pairing UI are verified to be functioning properly on real hardware, fully satisfying the requirements of Phase 2C. The foundation is robust and ready for Phase 3 (File Transfer Execution).
