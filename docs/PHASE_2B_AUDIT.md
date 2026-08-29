# Ferry Phase 2B Recovery & Verification Audit Report

**Date**: 2026-08-29  
**Auditor**: Lead Architect / Agent  
**Repository State**: Master branch (`b18c488` + Phase 2B working tree changes)  
**Status**: **PHASE 2B COMPLETE — READY FOR PHASE 2C**

---

## 1. Executive Summary

Phase 2B implements a robust, application-layer authenticated key exchange (AKE) and encrypted control channel between Arch Linux and Android over local TCP. All cryptographic primitives (Ed25519, X25519, HKDF-SHA256, ChaCha20-Poly1305) are fully implemented and verified on both platforms. 

- **53 Linux tests** passing (unit, cryptographic, and loopback control-plane integration).
- **Android unit tests** passing (session key derivation, SAS, AEAD roundtrip, state machine).
- **Physical Wi-Fi connection verified**: Android app (`Realme RMX3870`, SDK 36) and Arch Linux daemon (`archnoir`) successfully performed mutual handshake, derived matching SAS (`049968`), and established an encrypted AEAD session over physical Wi-Fi.

---

## 2. Audit Findings: Detailed Questions

### 2.1. Transport & Framing
1. **What secure transport is currently implemented?**  
   Application-layer framed binary stream over standard TCP (default port `53770`).
   - **Handshake Phase**: Plaintext frame consisting of 2-byte magic (`0x46 0x59` / `"FY"`), 4-byte big-endian payload length, and UTF-8 JSON message envelope (`protocol_version: 1`).
   - **Encrypted Session Phase**: AEAD binary frame consisting of 4-byte big-endian ciphertext length, 12-byte monotonic nonce, and ChaCha20-Poly1305 ciphertext + 16-byte Poly1305 authentication tag.
2. **Is it TLS, Noise, or another design?**  
   It is a custom application-layer Authenticated Key Exchange (AKE) protocol (Station-to-Station / SIGMA inspired), not standard TLS 1.3 or Noise library implementation.
3. **Is ChaCha20-Poly1305 used?**  
   **YES**. Linux uses `cryptography.hazmat.primitives.ciphers.aead.ChaCha20Poly1305`. Android uses `javax.crypto.Cipher.getInstance("ChaCha20-Poly1305")`.

### 2.2. Cryptographic Keys & Handshake
4. **How are session keys established?**  
   Both peers exchange ephemeral X25519 public keys and 16-byte random nonces in `HANDSHAKE_INIT` / `HANDSHAKE_RESPONSE`. Both compute the shared secret $dh\_shared = \text{X25519}(sk_{eph}, pk_{eph\_remote})$. HKDF-SHA256 derives distinct directional keys (`ferry-session-initiator-v1` and `ferry-session-responder-v1`) using a deterministic transcript salt containing both static keys, ephemeral keys, and nonces.
5. **Is ephemeral X25519 actually implemented?**  
   **YES**. Linux: `X25519PrivateKey.generate()`. Android: `KeyPairGenerator.getInstance("X25519")`. Ephemeral private keys are discarded immediately following HKDF derivation, providing forward secrecy.
6. **How are long-term device identities represented?**  
   Raw 32-byte Ed25519 public keys, represented in base64url encoding (no padding) on the wire and in configuration/database records.
7. **Is Ed25519 actually implemented?**  
   **YES**. Linux uses Python `cryptography` Ed25519. Android uses `java.security.KeyPairGenerator` (EdDSA/Ed25519, API 33+) and `java.security.Signature`.
8. **How is peer authentication currently performed?**  
   Mutual signature verification via `AUTH_CHALLENGE` / `AUTH_RESPONSE`. Each party signs the canonical transcript (`ferry-auth-v1|` + ordered public keys and nonces) with their Ed25519 identity key and verifies the peer's signature.
9. **How is the peer identity cryptographically bound to the session?**  
   The signed transcript contains both static Ed25519 public keys, both ephemeral X25519 public keys, and both nonces. Furthermore, the static identity public keys are part of the HKDF salt for session key derivation.

### 2.3. SAS & Pairing Behavior
10. **Is SAS implemented?**  
    **YES**. 3 bytes derived via HKDF-SHA256 with info `ferry-sas-v1` modulo $10^6$, formatted as a 6-digit decimal PIN.
11. **If SAS exists, is it currently auto-accepted/test-only?**  
    **YES**. In Phase 2B, both Linux and Android derive and log the SAS, but auto-accept pairing without blocking for user GUI interaction.
12. **Where are keys stored on Linux?**  
    - Identity private key: `$XDG_DATA_HOME/ferry/identity.key` (PEM PKCS#8, mode `0600`).
    - Paired public keys: `$XDG_DATA_HOME/ferry/ferry.db` (SQLite table `trusted_devices`).
13. **Where are Android keys stored?**  
    - Identity keypair: `SharedPreferences` (`ferry_identity.xml`, mode `Context.MODE_PRIVATE`).
    - Paired public keys: `SharedPreferences` (`ferry_trust_store.xml`, mode `Context.MODE_PRIVATE`).
14. **Is Android Keystore actually being used?**  
    **NO**. As planned for Phase 2B, Android keys are currently in private `SharedPreferences`. Hardware-backed `AndroidKeyStore` migration is scheduled for Phase 2C.

### 2.4. State Machine & Lifecycle
15. **What exact session state machine exists?**  
    `DISCONNECTED` $\to$ `CONNECTING` $\to$ `HANDSHAKING` $\to$ `PAIRING` (if new) / `AUTHENTICATING` (if known) $\to$ `ESTABLISHED` $\to$ `CLOSING` $\to$ `DISCONNECTED` (or `FAILED`).
16. **How are disconnects handled?**  
    Controlled closure via `DISCONNECT` envelope or clean socket close on TCP FIN/EOF, transitioning state to `CLOSING` then `DISCONNECTED`.
17. **How are reconnects handled?**  
    Public key is matched against the trust store. Known peers skip `PAIRING`, transition directly to `AUTHENTICATING`, verify Ed25519 signatures, update `last_seen` timestamp, and enter `ESTABLISHED`.
18. **How are unknown/untrusted devices handled?**  
    In Phase 2B, they enter `PAIRING` and auto-persist trust (to be gated behind interactive UI confirmation in Phase 2C).
19. **Is there any insecure plaintext fallback?**  
    **NO**. No plaintext fallback exists. After handshake, any non-AEAD or corrupt payload is rejected and closes the connection.
20. **Is there any test-mode or auto-accept path?**  
    The auto-accept path is the default Phase 2B path for untrusted peers.
21. **If test mode exists, is it isolated from normal behavior?**  
    It is currently inlined in `service.py` and `FerryControlClient.kt` as provisional Phase 2B code.
22. **Does any production/default code accidentally bypass user trust?**  
    In Phase 2B, trust is persisted automatically upon successful signature verification without interactive user approval. This is the explicit boundary to be resolved in Phase 2C.

---

## 3. Security Analysis

| Aspect | Status | Finding |
| :--- | :--- | :--- |
| **Forward Secrecy** | **VERIFIED** | Ephemeral X25519 keys are generated per session and destroyed after key derivation. |
| **Identity Authentication** | **VERIFIED** | Ed25519 signatures over the full exchange transcript prove identity key ownership. |
| **Replay Protection** | **VERIFIED** | 96-bit monotonic nonce counters are verified on every AEAD frame; replayed frames are rejected. |
| **Replay / MITM Resistance** | **PROVISIONAL** | Cryptographic binding is complete. Full out-of-band MITM resistance requires the interactive SAS confirmation in Phase 2C. |

---

## 4. Test Verification Results

### 4.1. Linux Test Suite
```
Ran 53 tests in 4.240s
OK
```
- `test_config.py` (2 tests)
- `test_db.py` (4 tests)
- `test_discovery_models.py` (5 tests)
- `test_discovery_manager.py` (4 tests)
- `test_discovery_integration.py` (1 test)
- `test_protocol_models.py` (5 tests)
- `test_identity.py` (11 tests)
- `test_session.py` (16 tests)
- `test_control_plane.py` (5 tests)

### 4.2. Android Test Suite & Build
- `cd android && ./gradlew testDebugUnitTest` $\to$ **BUILD SUCCESSFUL**
- `cd android && ./gradlew assembleDebug` $\to$ **BUILD SUCCESSFUL**
- Test classes: `ProtocolConstantsTest`, `DiscoveredDeviceTest`, `SessionTest` (13 tests).

### 4.3. Physical End-to-End Verification
- Tested between Arch Linux (`10.213.207.51:53770`) and Realme RMX3870 (`10.213.207.31:53770`).
- Android initiated connection to Linux desktop.
- Handshake completed, SAS derived (`049968`), Ed25519 signatures verified.
- Reached `ESTABLISHED` state with `● Secure` badge on Android UI.

---

## 5. Documentation Inconsistencies Identified

1. **`docs/ARCHITECTURE.md`**: Section 1 mentions `TLS 1.3 / Noise Protocol`. Should be clarified as application-layer Authenticated Key Exchange with ChaCha20-Poly1305 AEAD.
2. **`docs/PROTOCOL.md`**: Mentions `PAIR_REQUEST` / `PAIR_CONFIRM` message envelopes. In Phase 2B, `AUTH_CHALLENGE` / `AUTH_RESPONSE` were implemented; Phase 2C will specify the exact interactive pairing decision message.

---

## 6. Phase Boundary & Next Steps

Phase 2B is **COMPLETE and FACTUALLY VERIFIED**.

### Remaining Work for Phase 2C (Do NOT implement in this task):
1. **Interactive SAS Pairing UI**:
   - Linux: GNOME notification / Libadwaita dialog with 6-digit SAS and Accept/Reject buttons.
   - Android: Compose pairing dialog/card with 6-digit SAS and Accept/Reject buttons.
2. **Interactive Decision Protocol**:
   - Transmit explicit user acceptance decision before calling `persistTrust()`.
3. **Android Keystore Migration**:
   - Move Ed25519 key generation from `SharedPreferences` to `AndroidKeyStore`.
