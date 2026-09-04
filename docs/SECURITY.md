# Ferry Security Architecture & Threat Model

This document establishes the security model, cryptographic principles, identity handling, and safe file I/O guidelines for **Ferry**.

---

## 1. Fundamental Principle: LAN is Untrusted

> [!CAUTION]
> **Core Axiom**: Being on the same Local Area Network (LAN) or Wi-Fi network does **NOT** grant trust. 
>
> Local networks are frequently shared, untrusted, or vulnerable to ARP spoofing, rogue DHCP servers, and malicious peers. Ferry treats the local network as an untrusted transport medium.

The following attributes are considered **ephemeral metadata and NEVER a security identity**:
* IP addresses
* MAC addresses
* Hostnames / mDNS names
* User-assigned device display names

---

## 2. Cryptographic Identity & Trust Pipeline

```
       Discovery (mDNS)
              │
              ▼
    Untrusted Ephemeral State
              │
              ▼
   Out-of-Band Pairing (SAS PIN)
              │
              ▼
Persistent Cryptographic Identity (Ed25519)
              │
              ▼
Authenticated Key Exchange (X25519 / TLS 1.3)
              │
              ▼
Authorized Operations & End-to-End Encryption
```

### 2.1. Cryptographic Primitives (No Custom Crypto)
Ferry relies strictly on industry-standard, well-audited primitives:
* **Identity & Signatures**: **Ed25519** (RFC 8032) for asymmetric device identity and mutual authentication.
* **Key Agreement**: **X25519** (RFC 7748) for Diffie-Hellman ephemeral key exchange.
* **Symmetric Encryption & AEAD**: **ChaCha20-Poly1305** (RFC 8439) or **AES-256-GCM** via TLS 1.3 transport.
* **Integrity & Hashing**: **SHA-256** (FIPS 180-4) for end-to-end file content digests.
* **Key Derivation**: **HKDF-SHA256** (RFC 5869).

---

## 3. Secure Device Pairing

Pairing transitions an unknown device from *untrusted* to *trusted*.

1. **Exchange Public Keys**: Both devices exchange their static Ed25519 identity public keys and ephemeral X25519 keys.
2. **Derive Short Authentication String (SAS)**: Both devices independently compute:
   $$\text{SAS} = \text{HKDF}(\text{transcript}, \text{salt}, \text{info}) \pmod{10^6}$$
   formatted as a 6-digit numeric PIN (e.g. `482 910`).
3. **Visual Out-of-Band Confirmation**: Both the Android screen and the Linux GNOME desktop display the computed PIN. The user must manually confirm that both numbers match before keys are persisted.
4. **Persistent Key Storage**:
   * **Linux**: Private keys and paired public keys stored in `$XDG_DATA_HOME/ferry/ferry.db` (file permissions `0600`).
   * **Android**: Asymmetric keys generated inside and protected by hardware-backed **Android Keystore** (`AndroidKeyStore`) where supported, with a secure software-backed `SharedPreferences` fallback on devices exhibiting OEM hardware crypto bugs (e.g., generating ECDSA keys when Ed25519 is requested).
---

## 4. Secure File Handling & Path Traversal Mitigations

Malicious peers might attempt to write files outside the intended destination directory (e.g. `../../.bashrc` or `../../../etc/shadow`).

### 4.1. Strict Filename Sanitization
Before saving any received file:
1. **Basename Extraction**: Strip all path components, drive letters, and directory separators (`/` and `\`).
2. **Reject Control Characters**: Discard or replace control characters (ASCII 0-31, 127), null bytes (`\0`), and unicode path-spoofing homoglyphs.
3. **Prevent Reserved Names**: Reject Windows/DOS reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1..9`, `LPT1..9`) and Unix special entries (`.`, `..`).
4. **Enforce Allowed Extensions / Length**: Truncate overly long names (max 255 bytes UTF-8) while preserving valid extensions.

### 4.2. Path Canonicalization & Confinement
On Linux:
```python
def resolve_safe_destination(download_dir: Path, filename: str) -> Path:
    sanitized = sanitize_filename(filename)
    dest_path = (download_dir / sanitized).resolve()
    if not dest_path.is_relative_to(download_dir.resolve()):
        raise SecurityError("Path traversal attempt detected")
    return dest_path
```

### 4.3. Atomic Staged Writing & Quarantine
1. Incoming streams write to a temporary file: `<sanitized_name>.<transfer_id>.part`.
2. As chunks arrive, the receiver incrementally computes the SHA-256 digest.
3. If the completed digest matches the sender's signed metadata, the file is atomically renamed to the final destination:
   $$\text{rename}(\text{file.part} \to \text{file})$$
4. If the connection breaks or the hash fails, the `.part` file is deleted immediately.

---

## 5. Security Threat Matrix

| Threat | Mitigation |
| :--- | :--- |
| **Man-in-the-Middle (MitM) on Wi-Fi** | Ephemeral Diffie-Hellman authenticated with static Ed25519 identity + SAS verification PIN. |
| **Rogue Discovery Spoofing** | Discovery metadata is unauthenticated; connection is rejected if cryptographic handshake fails. |
| **Path Traversal / Arbitrary File Overwrite** | Basename stripping, canonical path verification (`is_relative_to`), and atomic staging. |
| **Data Corruption / Tampering** | Streaming AEAD framing + End-to-end SHA-256 file digest verification. |
| **Denial of Service (OOM via huge frames)** | Hard frame limit of 1 MiB for control messages; streaming chunk buffers for data. |

---

## 6. Resumable Transfer Security (Phase 3E Design — Not Yet Implemented)

See `docs/PHASE_3E_DESIGN.md` Section 12 (Security Self-Review) and Section 6 (AEAD/Nonce Strategy) for the full analysis.

### Key invariants

- Resume messages (`TRANSFER_RESUME_REQUEST` etc.) are **only processed inside a fully ESTABLISHED, Ed25519-authenticated session**.
- The `transfer_id` alone is not a credential; peer identity is verified via the session handshake.
- Original session AEAD keys are **never reused** across reconnections. A resumed transfer uses fresh ephemeral keys.
- The receiver derives `resume_chunk_index` from its own locally persisted `bytes_received`. An attacker cannot supply a forged offset without also supplying the correct prefix SHA-256.
- A prefix SHA-256 cross-check (receiver hashes `.part`; sender hashes same-length source prefix) detects partial corruption and source-file replacement before resuming.
- Unpairing a peer immediately deletes all INTERRUPTED `.part` files for that peer and marks them FAILED.

### New threat entries

| Threat | Mitigation |
| :--- | :--- |
| **Replay of old interrupted transfer chunks** | New session = new ephemeral DH keys. Old ciphertext cannot be decrypted under new keys. |
| **Fake resume request from wrong peer** | Session Ed25519 authentication + `sender_identity` field comparison vs. DB record. |
| **Attacker-supplied forged resume offset** | Receiver offset is from local DB, not from sender. Prefix hash cross-check catches any inconsistency. |
| **Accumulation of stale .part files** | 7-day TTL; on-startup cleanup; explicit Discard UI action. |
| **Revoked peer resuming old transfer** | Unpair handler deletes all INTERRUPTED transfers for that device_id. |
