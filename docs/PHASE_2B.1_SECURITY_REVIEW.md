# Ferry Phase 2B.1: Independent Cryptographic Security Review

**Date**: 2026-08-29  
**Reviewer**: Lead Security Architect / AI Agent  
**Scope**: Cryptographic control-plane implementation (Phase 2B) across Linux and Android platforms.  
**Purpose**: Critically review the existing Phase 2B AKE and AEAD implementation before proceeding to Phase 2C (interactive pairing) and Phase 3 (file transfer).

## 1. Executive Summary

An independent source-level review of the Phase 2B cryptographic stack was conducted across the Linux (Python) and Android (Kotlin) implementations. The review verifies that the protocol successfully implements an Authenticated Key Exchange (AKE) and an AEAD session layer matching the architecture specifications. 

The implementation correctly uses industry-standard cryptographic primitives:
* **Ed25519** for long-term device identity.
* **X25519** for ephemeral Diffie-Hellman key exchange (providing Forward Secrecy).
* **HKDF-SHA256** for deterministic session key and SAS derivation.
* **ChaCha20-Poly1305** for AEAD (Authenticated Encryption with Associated Data) frame protection.

No critical security vulnerabilities were identified in the cryptographic math, primitive selection, or framing logic. Several moderate design limitations are present by design in Phase 2B (e.g., auto-accepting untrusted connections) which are correctly documented and deferred to Phase 2C. 

## 2. Implementation Analysis

### 2.1. Device Identity (Ed25519)
* **Linux**: Implemented securely via `cryptography.hazmat`. Private keys are stored in PEM format at `$XDG_DATA_HOME/ferry/identity.key` with restrictive `0600` permissions.
* **Android**: Uses standard Java `KeyPairGenerator("Ed25519")` (API 33+). Keys are currently stored in `SharedPreferences` (MODE_PRIVATE).
* **Finding**: Correct. Migration of Android keys to hardware-backed `AndroidKeyStore` is appropriately planned for Phase 2C to prevent extraction on rooted devices.

### 2.2. Ephemeral Key Exchange (X25519)
* **Implementation**: Both platforms generate fresh X25519 keypairs per session and successfully derive the shared secret.
* **Small Subgroup/Zero-Key Attacks**: The Python `cryptography` library strictly rejects exchange with all-zero public keys. Java's `KeyAgreement` correctly applies X25519 cofactor clearing (clamping) according to RFC 7748.
* **Forward Secrecy**: Private ephemeral keys are immediately discarded/garbage-collected after the DH shared secret is computed.
* **Finding**: Secure. Forward secrecy is guaranteed.

### 2.3. Key Derivation (HKDF-SHA256)
* **Implementation**: Uses `dh_shared` as Input Keying Material (IKM) and a deterministic transcript salt containing both static and ephemeral public keys + nonces.
* **Domain Separation**: Correctly uses distinct `info` strings (`ferry-session-initiator-v1`, `ferry-session-responder-v1`, `ferry-sas-v1`) to derive three independent keys from the same PRK.
* **Compliance**: Checked against RFC 5869. Both Python's `cryptography.hazmat` and the custom Android HMAC-SHA256 expand/extract loop correctly implement the standard.
* **Finding**: Secure and standard-compliant.

### 2.4. Authentication (Transcript Signatures)
* **Implementation**: Mutual authentication is achieved by exchanging `AUTH_CHALLENGE`/`AUTH_RESPONSE` messages containing Ed25519 signatures.
* **Transcript Binding**: The signed message is `ferry-auth-v1|` appended with the full fixed-length public transcript (static keys, ephemeral keys, and nonces). 
* **Finding**: Secure. Binding the signature to both the ephemeral keys and random nonces thoroughly prevents cross-session replay and Man-in-the-Middle (MitM) relay attacks.

### 2.5. AEAD Framing (ChaCha20-Poly1305)
* **Implementation**: Post-handshake frames are encrypted. The cipher uses a 96-bit (12-byte) nonce composed of 4 zero bytes and an 8-byte big-endian monotonic counter.
* **Replay Protection**: The receiver strictly tracks the expected counter. Any mismatch or replay immediately throws an error and drops the session.
* **Finding**: Secure. 

### 2.6. Trust Persistence & State Machine
* **Implementation**: Peer trust is keyed and looked up by the Ed25519 static public key (Base64url), **not** the easily-spoofed `device_id` UUID.
* **Finding**: Secure. Relying on cryptographic identity rather than broadcasted UUIDs prevents impersonation attacks.

## 3. Identified Risks & Recommendations

The following items do not represent immediate implementation flaws but are architectural properties to be addressed in subsequent phases.

1. **Unencrypted Frame Length Prefix (DoS Vector)**:
   * **Issue**: In the AEAD frame, the 4-byte ciphertext length is sent in plaintext and is not included as Associated Data (AAD) in the Poly1305 tag.
   * **Impact**: An active attacker can flip bits in the length prefix. The receiver will then read the wrong amount of data from the TCP stream, causing the Poly1305 MAC check to fail and dropping the connection (TCP desynchronization).
   * **Recommendation**: Acceptable risk. Since Ferry runs over TCP, stream tampering already causes connection drops. No confidentiality or integrity of the payload is compromised.

2. **Identity Privacy (Observability)**:
   * **Issue**: `HANDSHAKE_INIT` and `AUTH_CHALLENGE` messages are sent in plaintext. Eavesdroppers on the local network can observe the Ed25519 public keys and device names of pairing parties.
   * **Impact**: Lack of anonymity. 
   * **Recommendation**: Acceptable for a local network file transfer utility. The public key is not sensitive material.

3. **Auto-Persisting Trust (Deferred to Phase 2C)**:
   * **Issue**: The Phase 2B codebase automatically persists trust for any new peer that completes the cryptographic handshake.
   * **Impact**: Vulnerable to MITM until interactive pairing is introduced.
   * **Recommendation**: This is a known, documented limitation. Phase 2C must strictly enforce the interactive GNOME/Compose pairing dialog to verify the 6-digit SAS before `persistTrust()` is called.

## 4. Conclusion

The Phase 2B control-plane codebase is cryptographically sound, relies exclusively on audited primitives, and correctly applies them to build an Authenticated Key Exchange. The protocol provides forward secrecy, mutual authentication, and AEAD stream protection. 

The project is safely ready to proceed to **Phase 2C (Trust & Pairing UX)** without architectural redesigns.
