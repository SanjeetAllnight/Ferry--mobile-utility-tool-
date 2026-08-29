# Ferry Phase 2C Final Reconciliation and Audit

**Date:** 2026-08-30
**Status:** Phase 2C Verified & Complete

This document resolves final documentation inconsistencies regarding the physical verification of Phase 2C and provides a factual, reconciled view of the current repository state based on the physical Android device.

## 1. Physical Device Identity
- **Device Model:** Realme RMX3870
- **Android OS Version:** Android 16
- **SDK Version:** API 36
- **Installed Ferry Version:** `0.1.0-phase1`

## 2. Actual Android Identity Storage Path
- **Implementation Checked:** `FerryIdentity.kt`
- **Path Taken on Physical Device:** Software Ed25519 with `SharedPreferences` (Context.MODE_PRIVATE) fallback.
- **Trigger:** The fallback is triggered conditionally. The code attempts hardware-backed `AndroidKeyStore` generation first. It then validates the generated public key size (expecting 44 bytes). On this specific device, a known OEM bug silently generates a 91-byte `secp256r1` ECDSA key instead.
- **Fallback Execution:** When the 91-byte key is detected, the implementation deletes the corrupted `AndroidKeyStore` entry and falls back to generating a software Ed25519 keypair using the default Conscrypt provider.
- **Storage:** The software keypair is stored in Android's standard `SharedPreferences` in `Context.MODE_PRIVATE`.

## 3. Verified Security Level
- **Security Claim:** The Android identity key on this specific device is **software-backed**.
- **Documentation:** The `SECURITY.md` and `PHASE_2C_REPORT.md` files have been corrected. They no longer claim unconditional hardware backing or `EncryptedSharedPreferences`. They now accurately state that Ferry uses `AndroidKeyStore` where supported, and gracefully falls back to software `SharedPreferences` when OEM bugs are detected.

## 4. Identity Persistence and Stability
- **Stability:** The identity public key remains completely stable across application restarts.
- **Mechanism:** On startup, the `loadOrGenerateKeyPair()` routine checks for an existing hardware key. Not finding one, it proceeds to the generation logic, which again encounters the OEM bug, fails, and gracefully reaches the fallback path. The fallback path correctly detects the existing software keys in `SharedPreferences` and loads them instead of rotating.

## 5. Pairing Trust Lifecycle
- **Initial Pairing:** SAS (Short Authentication String) is successfully generated, displayed on both devices, and requires mutual confirmation.
- **Trust Persistence:** Trust is only persisted in the `FerryTrustStore` after the user successfully taps "Accept".
- **Trusted Reconnection:** Disconnecting and reconnecting immediately re-establishes a secure connection (`ESTABLISHED` / `● Secure` UI), completely bypassing the SAS screen.

## 6. Test Suite Results
- **Linux Test Suite:** 53 tests run — **All Passed**.
- **Android Unit Tests:** Run via `testDebugUnitTest` — **Passed**.
- **Android APK Build:** Run via `assembleDebug` — **Passed**.

## 7. Documentation Corrections Made
1. **`docs/PROJECT_STATE.md`:** Fixed Android version reference. Marked Phase 2C as "Complete / Verified" and set Phase 3 to "Pending Start".
2. **`docs/SECURITY.md`:** Clarified that Android keys are hardware-backed *where supported*, with an explicit software fallback path for buggy OEM firmware.
3. **`docs/PHASE_2C_REPORT.md`:** Corrected Android version from 14 to 16. Corrected SharedPreferences path to reflect standard `Context.MODE_PRIVATE` instead of `EncryptedSharedPreferences`.

## 8. Remaining Security Limitations
- On devices exhibiting the OEM hardware KeyStore bug (like the Realme RMX3870), identity keys rely on software-backed `SharedPreferences`. While `Context.MODE_PRIVATE` restricts access to the Ferry app UID, a root-level compromise of the Android device would expose the Ed25519 identity private key material.
- End-to-end security over the untrusted LAN relies completely on this keypair.

## 9. Final Phase 2C Status
**Complete**. The interactive trust, authentication, UI components, and cryptographic storage pipelines are solid and verified on real hardware. We are fully ready to execute Phase 3 (File Transfer).
