# Phase 3E Design — Resumable Transfer Architecture

**Date:** 2026-09-05  
**Status:** DESIGN ONLY — No production code modified  
**Scope:** Architecture, protocol, persistence, security, and implementation plan

---

## 1. Problem Statement

Ferry currently deletes the `.part` file on any interruption and requires the user to restart from byte 0. For large files (video, backups, disk images), this is a poor experience. Phase 3E adds a safe, authenticated, replay-resistant resume mechanism that does not weaken the existing security model.

---

## 2. Core Design Decisions

### 2.1. Resume Identity: Preserve the Original Transfer ID

**Decision:** The original `transfer_id` (UUIDv4) is **reused** across a resume. It is the stable anchor for all resume state: the `.part` file name, the database record, and the resume negotiation message.

**Rationale:**
- Avoids a new ID/old-file lookup problem.
- Keeps the DB schema minimal (one row per logical transfer).
- The existing `transfer_id` is already a globally unique UUIDv4 that the sender generated and both parties stored.

**Security:** The `transfer_id` alone is **never sufficient** to authenticate a resume. It is a lookup key, not a credential. Authentication is provided by the session handshake (Ed25519 + X25519 ephemeral), which already verifies peer identity before any resume message is processed.

### 2.2. What Survives Interruption: Persisted Resumable State

When a transfer is interrupted (session disconnect, network failure), instead of deleting the `.part` file, Ferry transitions the transfer to a new terminal-like state: **INTERRUPTED**.

In INTERRUPTED state:
- The `.part` file is **kept** (not deleted).
- A database record is updated (or inserted) with `status = INTERRUPTED`, the `bytes_received`, `chunk_index`, and the original `TransferMetadata` (file_name, file_size, sha256, chunk_size, chunk_count).
- The `.part` file is **not renamed** and **not exposed** to the user as a file.

### 2.3. Resume Only Within Authenticated Session

Resume negotiation happens **only inside a fresh, fully authenticated Ferry session** (ESTABLISHED state). This is a hard invariant. No resume message is processed before authentication completes.

---

## 3. Protocol Changes

### 3.1. New Message Types

All new messages use the existing `FerryEnvelope` JSON framing (FY magic + uint32 length + JSON).

#### `TRANSFER_RESUME_REQUEST` (receiver → sender)

Sent by the receiver (the side that has the `.part` file) after an authenticated session is established, to resume an interrupted incoming transfer.

```json
{
  "type": "TRANSFER_RESUME_REQUEST",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "resume_offset_bytes": 7340032,
    "resume_chunk_index": 112,
    "partial_sha256": "aabbcc...64hex",
    "protocol_version": 1
  }
}
```

Fields:
- `transfer_id`: UUIDv4 — the original transfer ID.
- `resume_offset_bytes`: The number of bytes the receiver has safely written (= `chunk_index × chunk_size` for full chunks; verified by the receiver from local state, never trusted from sender).
- `resume_chunk_index`: The index of the next chunk the receiver expects (0-based).
- `partial_sha256`: SHA-256 of the data written so far. The sender verifies this matches the prefix of the source file.
- `protocol_version`: Must be `1`.

**Authentication binding:** This message is sent over an already-ESTABLISHED (Ed25519-authenticated) session. The session identity of the peer is verified before this message is processed. No additional credential in the message payload is required; the session provides authentication. However, the sender **must** verify the `transfer_id` belongs to a transfer originally negotiated with this exact peer identity.

#### `TRANSFER_RESUME_ACCEPT` (sender → receiver)

Sender confirms the resume is valid and will restart streaming from the given offset.

```json
{
  "type": "TRANSFER_RESUME_ACCEPT",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "resume_chunk_index": 112,
    "protocol_version": 1
  }
}
```

Fields:
- `transfer_id`: The original transfer UUID.
- `resume_chunk_index`: The chunk index from which the sender will start sending (must match the receiver's request).

#### `TRANSFER_RESUME_REJECT` (sender → receiver)

Sender cannot or will not resume. Receiver should transition to FAILED and clean up.

```json
{
  "type": "TRANSFER_RESUME_REJECT",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "reason": "SOURCE_MODIFIED"
  }
}
```

Reason codes: `SOURCE_MODIFIED`, `TRANSFER_NOT_FOUND`, `PARTIAL_CORRUPT`, `WRONG_PEER`, `STALE`, `PEER_CANCELLED`.

#### Modified `TRANSFER_CHUNK` Frame (no wire format change)

The existing binary FYCH frame carries the chunk seq number. During a resume, the `seq` field in the FYCH frame starts at `resume_chunk_index`, not 0. The receiver's `_next_seq` is initialized from its local persisted state. **No change to the binary frame format is needed.**

---

### 3.2. Resume Flow (Receiver-Initiated)

```
RECONNECT / ESTABLISHED SESSION
        │
        ▼
Receiver checks DB for INTERRUPTED transfers from this peer
        │ (has INTERRUPTED transfer)
        ▼
Receiver computes SHA-256 of .part file
        │
        ▼
Receiver sends TRANSFER_RESUME_REQUEST
        │
        ├─── Sender: verifies transfer_id belongs to this peer ───────────────┐
        │    Sender: verifies source file unchanged (size + sha256)           │
        │    Sender: computes SHA-256 of first N bytes (prefix hash)         │
        │    Sender: compares prefix hash with receiver's partial_sha256      │
        │                                                                      │
        │    ┌─── Mismatch ───────────────────────────────────────────────────┘
        │    │                                                              │
        │    ▼                                                              │
        │  TRANSFER_RESUME_REJECT                              TRANSFER_RESUME_ACCEPT
        │        │                                                          │
        │        ▼                                                          ▼
        │  Receiver: delete .part,              Receiver: RESUMING state
        │  FAILED history                       Receiver: seek .part file to end
        │                                       Sender: skip first N chunks
        │                                       Sender: begin streaming at chunk N
        │                                               (seq = resume_chunk_index)
        │                                               ▼
        │                                       Normal TRANSFERRING flow
        │                                               ▼
        │                                       TRANSFER_COMPLETE → finalise()
        │                                               ▼
        │                                       Final full-file SHA-256 verified
        │                                               ▼
        │                                       TRANSFER_RESULT → COMPLETED
```

---

## 4. Extended State Machine

### New States

```
INTERRUPTED  — Transfer stopped mid-flight; .part file retained; resumable
RESUME_REQUESTED — Receiver sent TRANSFER_RESUME_REQUEST; awaiting sender decision
RESUMING     — Resume accepted; chunks flowing from resume_chunk_index
```

### Full State Machine (with resume extensions)

```
IDLE
  │
  ├─(TRANSFER_REQUEST sent/received)──► REQUESTED
  │                                          │
  │                         ┌────────────────┤
  │                  ACCEPT │         REJECT │ or CANCEL or TIMEOUT_EXPIRED
  │                         ▼                ▼
  │                    ACCEPTED          REJECTED / CANCELLED
  │                         │
  │                (begin())▼
  │                   TRANSFERRING
  │                    │       │
  │                    │       ├─(disconnect/IO error)──► INTERRUPTED
  │                    │       │                               │
  │                    │       ├─(CANCEL)──► CANCELLING        │ user taps Resume
  │                    │       │                 │             │
  │                    │       │                 ▼             ▼
  │                    │       │            CANCELLED    RESUME_REQUESTED
  │                    │       │                               │
  │                    │       │               ┌───────────────┤
  │                    │       │         ACCEPT│         REJECT│ or STALE
  │                    │       │               ▼               ▼
  │                    │       │           RESUMING         FAILED
  │                    │       │               │ (chunks flowing)
  │                    │       │               │
  │                    │       └───────────────┤
  │                    │                       │
  │                    └──► COMPLETED ◄────────┘
  │                         │
  │                    SHA-256 fail
  │                         ▼
  │                       FAILED
```

**Terminal states** (no transitions out): `COMPLETED`, `FAILED`, `CANCELLED`, `REJECTED`  
**New transitions:**
- `TRANSFERRING → INTERRUPTED` (network disconnect, IO error where we choose to preserve partial state)
- `INTERRUPTED → RESUME_REQUESTED` (user initiates resume)
- `INTERRUPTED → FAILED` (user discards, expiry, or peer unpaired)
- `RESUME_REQUESTED → RESUMING` (TRANSFER_RESUME_ACCEPT received)
- `RESUME_REQUESTED → FAILED` (TRANSFER_RESUME_REJECT received or timeout)
- `RESUMING → COMPLETED` (SHA-256 verified)
- `RESUMING → INTERRUPTED` (disconnect again — keep .part, retry again later)
- `RESUMING → FAILED` (IO error during resume)

---

## 5. Partial File & Integrity Model

### 5.1. Identification Without Trusting Filenames

The `.part` file is identified by transfer_id only:

```
staging/<transfer_id>.part
```

This is already the current naming convention. **No change required.** The filename is derived from a server-generated UUIDv4, not from any attacker-controlled value.

### 5.2. Partial Integrity Verification (Prefix Hash)

On resume request, the receiver computes **SHA-256 of the `.part` file as-is** (all bytes written so far) and sends it as `partial_sha256`.

The sender independently computes SHA-256 of the **first `resume_offset_bytes` bytes** of the source file. If the two hashes match, the receiver's partial state is consistent with the sender's source.

This is correct and sufficient because:
1. If any byte in the received data was corrupted, the partial hash will differ.
2. If the source file changed, the partial hash of the source prefix will differ from the receiver's hash.
3. The SHA-256 computation cannot be bypassed — the receiver must provide the correct hash, which requires possession of the actual `.part` bytes.

**Cost:** Hashing the partial file and the source prefix each require one sequential read. For a 5 GiB file interrupted at 50%, this is a ~2.5 GiB read on each side. This is acceptable for background processing — it is a one-time cost per resume, not per chunk.

### 5.3. No Chunk-Level Hashes / No Manifest

Adding per-chunk hashes would complicate the wire format and the receiver state machine without providing security benefits beyond the AEAD session integrity (each chunk frame is already authenticated by ChaCha20-Poly1305). The final full-file SHA-256 verification remains the canonical integrity check.

### 5.4. Final Full-File SHA-256 Verification

**Unchanged.** After all chunks are received (including resumed chunks), `finalise()` computes SHA-256 over the complete `.part` file and compares it to `meta.sha256`. This provides end-to-end integrity regardless of how many resume cycles occurred.

---

## 6. AEAD / Nonce Strategy

### Current Model

Each session uses a fresh ephemeral key derived from a new X25519 DH exchange. The AEAD nonce is a monotonically incrementing counter per session. This provides replay protection within a session.

### Resume Model

> [!CAUTION]
> **Original session keys are NOT reused.** A resumed transfer uses the fresh session keys derived from the new connection's ephemeral DH exchange.

**Rationale:**
- Reusing old session keys across reconnections would require persisting private key material outside the ephemeral session scope, which violates the security model and creates a large attack surface.
- There is **no nonce reuse risk**: a new session derives new keys from a new ephemeral DH exchange.
- Each resumed chunk gets a fresh nonce under the new session key.

**Chunk seq numbers and AEAD:** The FYCH `seq` field in resumed chunks starts at `resume_chunk_index` (e.g., 112), not 0. The receiver's `_next_seq` is initialized to `resume_chunk_index`. This is purely a data-plane ordering mechanism; replay protection is provided by the AEAD session nonce counter (which starts at 0 for the new session), not by the FYCH seq number.

**Binding resumed transfer to authenticated session:** The `sender_identity` in the original `TransferMetadata` (stored in the DB) is compared to the current session's authenticated peer Ed25519 public key. This binds the resume to the same logical peer.

**Summary of nonce invariants:**
- Session nonce counter: always starts at 0 for each new session — no reuse.
- FYCH seq: starts at `resume_chunk_index` for the resumed stream — no reuse of chunk data.
- No AEAD key material is persisted between sessions.

---

## 7. Persistence Model

### 7.1. Linux: Extended `transfer_history` Schema

The existing `transfer_history` table is extended with new columns (schema migration to version 3):

```sql
ALTER TABLE transfer_history ADD COLUMN interrupted_at INTEGER;           -- ms epoch; null if not interrupted
ALTER TABLE transfer_history ADD COLUMN bytes_received INTEGER;           -- bytes safely written to .part
ALTER TABLE transfer_history ADD COLUMN resume_chunk_index INTEGER;      -- next expected chunk seq; null if not interrupted
ALTER TABLE transfer_history ADD COLUMN partial_sha256 TEXT;             -- SHA-256 of .part at interruption time; null if not interrupted
ALTER TABLE transfer_history ADD COLUMN sender_identity TEXT;            -- Ed25519 pub key of original sender (base64url)
ALTER TABLE transfer_history ADD COLUMN original_metadata_json TEXT;     -- full TransferMetadata JSON blob
ALTER TABLE transfer_history ADD COLUMN expire_at INTEGER;               -- ms epoch; auto-delete .part if now > expire_at
```

> [!NOTE]
> Rationale for extending `transfer_history` rather than a new table: A resume is a continuation of the same logical transfer. Having two rows for one file (one INTERRUPTED, one COMPLETED) would be confusing. The original row is updated in place.

**Schema migration path:** `db.py` already uses `DB_SCHEMA_VERSION` and a `schema_version` table. A migration from version 2 → 3 adds the new columns with `ALTER TABLE ... ADD COLUMN` (safe, backward compatible, nulls for existing rows).

### 7.2. Android: Room / DataStore Persistence

Android currently holds transfer history in `_transferHistory: MutableStateFlow<List<TransferHistoryEntry>>` (in-memory, lost on process death).

For resumable transfers, a new **Room database** (or extension of existing Jetpack DataStore) must persist at minimum:
- `transfer_id` (String, primary key)
- `file_name`, `file_size`, `sha256`, `chunk_size`, `chunk_count` (from TransferMetadata)
- `status` (INTERRUPTED / COMPLETED / FAILED / CANCELLED)
- `bytes_received` (Long)
- `resume_chunk_index` (Int)
- `partial_sha256` (String)
- `peer_identity` (String — Ed25519 public key base64url)
- `content_uri` (String — SAF URI of the source file; only relevant for outgoing Android transfers)
- `interrupted_at` (Long)
- `expire_at` (Long)

**SAF note:** Android SAF URIs (`content://...`) may become invalid after process restart if the source app revokes the persistent URI grant. When preparing a resume on Android, the app must:
1. Check if the URI is still openable via `ContentResolver.openInputStream()`.
2. Verify file size and compute prefix hash.
3. If the URI is stale, reject the resume from the sender side.

This requires **persistable URI permissions**: `contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)` at the time of original send, before resumability is assumed.

### 7.3. Retention Policy

| Event | Action |
|---|---|
| Interruption | `.part` retained; `status=INTERRUPTED`; `expire_at = now + 7 days` |
| User taps Discard | `.part` deleted; `status=FAILED` |
| `expire_at` reached | `.part` deleted; `status=FAILED` (cleanup on service start) |
| User unpairs peer | All INTERRUPTED transfers for that peer: `.part` deleted; `status=FAILED` |
| App/device restart | `.part` retained on disk; DB record persisted; resume available after reconnect |
| Source file modified (detected on resume) | Sender sends TRANSFER_RESUME_REJECT; receiver deletes `.part`; `status=FAILED` |

**Maximum disk consumption:** One `.part` file per interrupted transfer per peer. By default: 7-day TTL, max 5 concurrent INTERRUPTED transfers per peer (configurable). On startup, the service scans for `.part` files that have no corresponding INTERRUPTED DB record (orphans from a crash before DB write) and deletes them.

---

## 8. Source File Change Detection

### Linux (Outgoing)

Before accepting a resume request, the sender:
1. Looks up the stored `original_metadata_json` to retrieve `file_size` and `sha256`.
2. `stat()` the original source path: if `st_size` or `st_mtime` differs from stored values, source may have changed.
3. Computes SHA-256 of the first `resume_offset_bytes` bytes of the source file.
4. Compares with `partial_sha256` from the resume request.
5. If any check fails → `TRANSFER_RESUME_REJECT` with `SOURCE_MODIFIED`.

**Source path:** The sender must persist the original source file path in the DB record at the time of the original transfer. If the file has been moved or deleted, the resume is rejected.

### Android (Outgoing)

1. Use the persisted SAF content URI.
2. Check `ContentResolver.openInputStream()` succeeds.
3. Query `OpenableColumns.SIZE` to verify file size unchanged.
4. Compute SHA-256 of the first `resume_offset_bytes` bytes via `openInputStream()`.
5. If any check fails → `TRANSFER_RESUME_REJECT`.

**Important:** Android cannot reliably detect in-place file modification via SAF. If the content provider reports the same size but content has changed, the hash check will detect the mismatch (via the prefix hash comparison).

---

## 9. User Experience Design (Conceptual)

### Pending Resumes Panel

```
Interrupted Transfers
────────────────────────
 📄  video.mp4                archnoir
     8.3 GB / 10 GB  (83%)
     Interrupted 5 min ago
     [Resume]  [Discard]

 📄  backup.tar.gz            archnoir
     2.1 GB / 4.5 GB  (47%)
     Interrupted 2 hours ago
     Expires in 6 days
     [Resume]  [Discard]
```

- **Resume** button: user-initiated; triggers TRANSFER_RESUME_REQUEST flow.
- **Discard** button: deletes `.part`, marks `status=FAILED`.
- The panel is shown only when INTERRUPTED transfers exist.
- Auto-resume (without user action) is **not** implemented initially; always requires explicit user tap.

### Seamless Resume (Optional Future Enhancement)

If both sides support protocol version 2, a session ESTABLISHED event can automatically trigger resume negotiation for all INTERRUPTED transfers from that peer — without user action. This is deferred to a future phase.

---

## 10. Concurrency

**Rule:** Same as current — one active transfer per session at a time. Multiple INTERRUPTED transfers may exist in the DB (they are not active). The user selects which one to resume. Multiple simultaneous resumes are not supported in Phase 3E.

If the user requests a fresh transfer while an INTERRUPTED transfer for the same file exists, Ferry should prompt: *"You have an interrupted transfer of 'video.mp4'. Resume it or start fresh?"*

---

## 11. Database Schema Migration

```python
# db.py — migration from schema version 2 to 3
if current_version < 3:
    for col, type_, default in [
        ("interrupted_at",        "INTEGER", "NULL"),
        ("bytes_received",        "INTEGER", "NULL"),
        ("resume_chunk_index",    "INTEGER", "NULL"),
        ("partial_sha256",        "TEXT",    "NULL"),
        ("sender_identity",       "TEXT",    "''"),
        ("original_metadata_json","TEXT",    "''"),
        ("source_path",           "TEXT",    "''"),   # Linux sender side
        ("expire_at",             "INTEGER", "NULL"),
    ]:
        try:
            cursor.execute(
                f"ALTER TABLE transfer_history ADD COLUMN {col} {type_} DEFAULT {default}"
            )
        except Exception:
            pass  # column already exists
```

---

## 12. Security Self-Review

| Question | Answer |
|---|---|
| **How is a resumed transfer authenticated?** | Resume messages are only processed inside a fully ESTABLISHED session (Ed25519 + X25519 ephemeral DH authenticated). The peer identity in the session is compared to `sender_identity` stored in the DB for that `transfer_id`. |
| **How is the original transfer bound to the resumed session?** | `transfer_id` lookup in DB; `sender_identity` comparison with current session's authenticated peer key; `partial_sha256` prefix hash cross-verification. |
| **How are old chunks prevented from being replayed?** | New session keys (new DH exchange) make old ciphertext decrypted with old keys invalid. The AEAD nonce counter starts at 0 for the new session. Any ciphertext from the old session is simply not valid under the new key. |
| **How are AEAD nonces kept unique?** | Each session derives fresh keys; nonce counter resets to 0 per session. No key reuse across sessions. |
| **How is the resume offset authenticated?** | The receiver derives `resume_chunk_index` from its locally persisted `bytes_received` (never from sender). The sender verifies via the prefix hash. An attacker cannot forge a higher offset without knowing the correct SHA-256 of that many bytes of the source file. |
| **How is partial-file corruption detected?** | The receiver computes SHA-256 of the `.part` file and sends it. The sender computes SHA-256 of the same-length prefix of the source file. Mismatch → `TRANSFER_RESUME_REJECT` with `PARTIAL_CORRUPT`. Final full-file SHA-256 at `finalise()` provides a second layer. |
| **How is source-file replacement detected?** | Sender checks `st_size`, `st_mtime`, and prefix hash before accepting resume. |
| **Can a revoked peer resume an old transfer?** | No. On unpair, all INTERRUPTED transfers for that peer are immediately cleaned up (`.part` deleted, status FAILED). Even if the `.part` file survived, the next connection would fail the Ed25519 handshake. |
| **Can transfer state survive application restart safely?** | Yes. The DB record is written before the session closes. On next session, the DB is read. The `.part` file is on persistent storage. No race between in-memory state and disk. |
| **Can stale transfer state be abused to write arbitrary files?** | No. The `.part` file path is `staging/<transfer_id>.part` (UUID-derived). The `final_path` is resolved from the sanitised filename in `original_metadata_json`, which is validated through the same `resolve_safe_destination()` path-confinement check. No attacker-supplied path escapes the staging directory. |

---

## 13. Failure Matrix

| Scenario | Receiver Action | Sender Action | Final State |
|---|---|---|---|
| Disconnect before any data | Do not retain .part (never opened) | Record FAILED | FAILED |
| Disconnect mid-transfer | Retain .part; DB: INTERRUPTED | Record INTERRUPTED (if sender) or FAILED (if receiver only) | INTERRUPTED |
| Disconnect near completion | Retain .part; DB: INTERRUPTED | Same | INTERRUPTED |
| App restart (both sides) | .part survives; DB persisted; resume available | Source path persisted | INTERRUPTED → user resumes |
| Device restart | Same as app restart | Same | INTERRUPTED → user resumes |
| Source file modified | Detected by sender prefix hash mismatch | Send RESUME_REJECT | FAILED; .part deleted |
| Partial file corrupted | Detected by sender prefix hash mismatch | Send RESUME_REJECT | FAILED; .part deleted |
| Peer unpaired | On unpair: .part deleted | N/A | FAILED |
| Peer re-paired (new SAS) | New pair = new device_id in DB; old INTERRUPTED transfers cleaned up | Same | FAILED; .part deleted |
| Stale resume request (expired) | expire_at exceeded; .part deleted | N/A | FAILED |
| Duplicate resume request | Idempotent: DB already INTERRUPTED; second TRANSFER_RESUME_REQUEST is a no-op | Respond with same RESUME_ACCEPT/REJECT | No change |
| Simultaneous resume attempts | Not supported; second request rejected with ERR_BUSY | N/A | Second attempt: FAILED |
| User cancels interrupted transfer | .part deleted; status FAILED | N/A | FAILED |
| Receiver storage unavailable during resume | TRANSFER_RESUME_REJECT; FAILED | N/A | FAILED |
| Wrong peer attempts resume | Session auth fails (Ed25519); or sender_identity mismatch → RESUME_REJECT | N/A | FAILED |
| Resumed transfer interrupted again | Retain (updated) .part; update bytes_received; remain INTERRUPTED | Record updated INTERRUPTED | INTERRUPTED → can retry |
| Source URI stale (Android SAF) | N/A | Cannot open URI; RESUME_REJECT `SOURCE_MODIFIED` | FAILED |
| 7-day expiry reached | On service start: .part deleted; status FAILED | N/A | FAILED |

---

## 14. Backward Compatibility

### Protocol Versioning

The existing `protocol_version: 1` field in `TRANSFER_REQUEST` and the `HANDSHAKE_INIT` already carries version information.

**Strategy:** The new resume message types (`TRANSFER_RESUME_REQUEST`, `TRANSFER_RESUME_ACCEPT`, `TRANSFER_RESUME_REJECT`) are **new optional message types**. An old peer that does not understand them will respond with `ERR_UNKNOWN_MESSAGE` (already defined in the protocol).

**Compatibility behavior:**
1. Before sending `TRANSFER_RESUME_REQUEST`, the receiver checks the peer's `protocol_version` from the handshake (future: use `capabilities` field in `HANDSHAKE_INIT`).
2. If peer does not support resume (old protocol version), the receiver falls back to prompting the user: *"Peer does not support resumable transfers. Start from the beginning?"*
3. Existing non-resumable transfers continue exactly as before — no existing message is changed.

**Recommended:** Add an optional `capabilities: ["resume"]` field to `HANDSHAKE_INIT`/`HANDSHAKE_RESPONSE` to signal resume support without bumping the entire protocol version.

---

## 15. Performance Considerations

| Operation | Cost | Notes |
|---|---|---|
| Prefix hash of N bytes (receiver) | O(N) one-time | Sequential read of .part file |
| Prefix hash of N bytes (sender) | O(N) one-time | Sequential read of source file |
| Sender skipping first N chunks | O(N) seek | `seek(resume_offset_bytes)` on the source file |
| DB write on interrupt | O(1) | Single SQL UPDATE |
| Startup .part scan | O(# files) | Fast glob; only compares filenames to DB |
| Resuming at chunk N | Zero extra overhead | Same streaming loop, different starting seq |

For a 10 GiB file interrupted at 50%, the prefix hash costs ~5 GiB of disk reads on each side (sender + receiver). This is unavoidable for integrity validation and should be performed on a background thread/coroutine with a progress indicator. An optimization for the future (not Phase 3E): store a rolling SHA-256 state in the DB at regular checkpoints (e.g., every 512 MB) to reduce rehashing cost.

---

## 16. Backward-Compatible `.part` File Handling

**Current behavior (Phase 3D):** `.part` files are always deleted on interruption.  
**New behavior (Phase 3E):** `.part` files are retained if `resume_eligible == True`.

Resume eligibility rules:
- `file_size > 0` (not zero-byte files)
- `bytes_received > 0` (at least one chunk received)
- Peer identity known in trust store
- No cancellation by user (CANCELLED → always delete .part)
- Not a failed integrity check (FAILED by SHA-256 → always delete .part)

If the service crashes before the DB record can be written, the `.part` file has no DB record. On startup, the cleanup scan finds `.part` files with no INTERRUPTED DB record and deletes them (existing Phase 3D behavior).

---

## 17. Implementation Breakdown (Next Phase Tasks)

### Task 1: Database Migration (Linux)
- Add new columns to `transfer_history` (schema version 3).
- Write `add_interrupted_transfer()`, `update_resume_progress()`, `get_interrupted_transfers_for_peer()`, `delete_interrupted_transfer()` in `db.py`.
- Write migration in `_ensure_schema()`.

### Task 2: Transfer State Machine (Linux)
- Add `INTERRUPTED`, `RESUME_REQUESTED`, `RESUMING` states to `TransferState`.
- Add transitions in `VALID_TRANSITIONS`.
- Modify `IncomingTransfer`: new `interrupt()` method (keeps .part, sets INTERRUPTED).
- Modify `IncomingTransfer`: new `resume(chunk_index)` method (re-opens .part in append mode, sets `_next_seq = chunk_index`, sets RESUMING).
- Modify `OutgoingTransfer`: `stream_chunks_from(offset)` variant that seeks to `offset` before streaming.

### Task 3: Service Layer (Linux)
- On session disconnect with an active TRANSFERRING transfer: call `interrupt()` instead of `cancel()` when the peer is known/trusted.
- On new ESTABLISHED session: query `get_interrupted_transfers_for_peer(peer_identity)`, emit to UI.
- Handle `TRANSFER_RESUME_REQUEST`: verify peer identity, source file, prefix hash → send ACCEPT/REJECT.
- Handle `TRANSFER_RESUME_ACCEPT`: transition IncomingTransfer to RESUMING; stream resumed chunks.
- Handle `TRANSFER_RESUME_REJECT`: delete .part; record FAILED.
- Add 120s timeout for TRANSFER_RESUME_ACCEPT response.

### Task 4: Protocol Models (Linux)
- Add `TRANSFER_RESUME_REQUEST`, `TRANSFER_RESUME_ACCEPT`, `TRANSFER_RESUME_REJECT` to `MessageType` enum and payload dataclasses.

### Task 5: UI (Linux)
- Add "Interrupted Transfers" section to the peer device card in `window.py`.
- Add Resume and Discard buttons.
- Resume button calls `service.request_resume(transfer_id)`.
- Discard button calls `service.discard_interrupted_transfer(transfer_id)`.

### Task 6: Android — Room Persistence
- Add Room entity `InterruptedTransferEntity` with all required fields.
- Add DAO methods.
- Wire to `FerryControlClient` state cleanup.

### Task 7: Android — Resume State Machine
- Add `INTERRUPTED`, `RESUME_REQUESTED`, `RESUMING` to `TransferState.kt`.
- Modify `FerryTransferReceiver`: add `interrupt()` and `resume(chunkIndex)`.
- Modify `FerryTransferClient`: add `streamChunksFromStream(inputStream, startChunk)` overload.

### Task 8: Android — Control Client
- On session disconnect with active transfer: call `interrupt()` if eligible.
- On new ESTABLISHED session: query Room DB for INTERRUPTED transfers; emit to UI.
- Handle `TRANSFER_RESUME_REQUEST` (incoming resume — Android is sender).
- Handle `TRANSFER_RESUME_ACCEPT` (Android is receiver, resume accepted).
- Handle `TRANSFER_RESUME_REJECT`.
- SAF URI re-validation and persistent URI permission takeover.

### Task 9: Android — UI
- Add "Interrupted Transfers" section to the connected-device Compose screen.
- Resume and Discard buttons.

### Task 10: Tests
- Unit: `IncomingTransfer.interrupt()`, `resume(n)`, `.part` retention.
- Unit: `OutgoingTransfer.stream_chunks_from(offset)` — skips correct number of chunks.
- Unit: Prefix hash comparison logic.
- Unit: `_record_interrupted_transfer()` DB write and read-back.
- Unit: `TRANSFER_RESUME_REQUEST` handler — correct ACCEPT/REJECT on various inputs.
- Unit: Wrong peer identity → REJECT.
- Unit: Expired transfer → FAILED.
- Unit: Discard clears DB + .part.
- Unit: Source file modified (size change) → REJECT.
- Android unit: `FerryTransferReceiver.interrupt()`, `resume()`.
- Android unit: Room persistence survives process restart (mock context).
- Integration: Full interrupt → reconnect → resume → COMPLETED cycle.

### Task 11: Documentation
- Update `PROTOCOL.md` with new message types and resume flow.
- Update `SECURITY.md` with resume threat model.
- Update `ARCHITECTURE.md` with INTERRUPTED state.
- Create `PHASE_3E_REPORT.md` after implementation.

---

## 18. Major Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Android SAF URI becomes stale after process restart | HIGH | Resume fails silently | Always validate URI before sending RESUME_ACCEPT; fail gracefully |
| Partial file silently corrupted by filesystem | LOW | Corrupt final file | Prefix hash cross-check + final full-file SHA-256 |
| Disk space exhausted by accumulated .part files | MEDIUM | New transfers blocked | 7-day TTL + max-transfer-count policy; show size in UI |
| Race between expiry cleanup and resume request | LOW | INTERRUPTED transfer vanishes | Atomic DB check-then-delete with locking |
| Session reconnects too fast for resume negotiation | LOW | Resume storm | 120s timeout on RESUME_ACCEPT; idempotent handling |
| User unpairs and re-pairs same device | MEDIUM | Old .part orphaned | Unpair handler explicitly cleans INTERRUPTED transfers for that device_id |
| Prefix hash computation blocks event loop | MEDIUM | UI freeze | Run prefix hash in asyncio executor / Dispatchers.IO |

---

## 19. Exact Next Implementation Task

The first concrete implementation task for Phase 3E is:

**Task 1: Linux DB migration (schema v3) + new INTERRUPTED state in TransferState + `IncomingTransfer.interrupt()` method.**

This provides the foundation for all other tasks. It is self-contained, fully testable in isolation, and does not affect any existing behavior (existing FAILED/CANCELLED paths are untouched; INTERRUPTED is only reached via the new `interrupt()` call path).

**Success criteria for Task 1:**
- `test_transfer_interrupt_retains_part_file` passes.
- `test_interrupted_transfer_db_record_persists` passes.
- All 161 existing Linux tests continue to pass.
- No existing FAILED/CANCELLED behavior changes.
