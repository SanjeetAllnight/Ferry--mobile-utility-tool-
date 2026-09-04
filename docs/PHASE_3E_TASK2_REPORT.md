# Phase 3E Task 2 — Resume Negotiation Protocol

**Date:** 2026-09-05  
**Status:** COMPLETE  
**Scope:** Protocol models · Wire payload validation · `IncomingTransfer.prepare_resume_request()` · Minimal service dispatch

---

## 1. New Message Types

Three new message types were added to `MessageType(str, Enum)` in [`linux/src/ferry_linux/protocol/models.py`](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/protocol/models.py):

* **`TRANSFER_RESUME_REQUEST`**: Sent by the receiver to the sender to negotiate resumption of an interrupted transfer.
* **`TRANSFER_RESUME_ACCEPT`**: Sent by the sender to the receiver confirming resume acceptance and specifying the chunk index at which streaming will resume.
* **`TRANSFER_RESUME_REJECT`**: Sent by the sender to the receiver declining the resume request with an explicit reason code.

All three message types adhere to the existing standard Ferry control framing:
```
[2B Magic 'FY'] + [4B uint32 big-endian payload length] + [UTF-8 FerryEnvelope JSON]
```
No secondary wire format was introduced; binary data frames continue to use the established `FYCH` binary layout.

---

## 2. Payload Models & Validation

### 2.1. `TransferResumeRequestPayload`

```json
{
  "type": "TRANSFER_RESUME_REQUEST",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "resume_offset_bytes": 7340032,
    "resume_chunk_index": 112,
    "partial_sha256": "4b5d6e7f...64hex_lowercase",
    "protocol_version": 1
  }
}
```

Validation rules enforced in `validate()` and `from_dict()`:
* **`transfer_id`**: Strict UUID format validation via `uuid.UUID`.
* **`resume_offset_bytes`**: Non-negative integer (`>= 0`) within reasonable bounds (`<= 10 GiB` limit).
* **`resume_chunk_index`**: Non-negative integer (`>= 0`).
* **`partial_sha256`**: Exactly 64 lowercase hexadecimal characters (`[0-9a-f]{64}`). Uppercase, non-hex, or incorrect-length strings are rejected.
* **`protocol_version`**: Must equal `1` (`PROTOCOL_VERSION`).
* **Offset / Chunk Consistency**:
  * `resume_offset_bytes == resume_chunk_index * chunk_size` (where `chunk_size = 65536`).
  * If `resume_chunk_index == 0`, `resume_offset_bytes` must be `0`.
* **Required fields**: Missing keys raise `ValueError`.

### 2.2. `TransferResumeAcceptPayload`

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

Validation rules:
* Valid UUID `transfer_id`.
* Non-negative `resume_chunk_index`.
* `protocol_version == 1`.
* All fields required.

### 2.3. `TransferResumeRejectPayload` & `ResumeRejectReason`

```json
{
  "type": "TRANSFER_RESUME_REJECT",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "reason": "SOURCE_MODIFIED"
  }
}
```

Strictly constrained reason codes defined in `ResumeRejectReason(str, Enum)`:
* `SOURCE_MODIFIED`: Source file size, modification timestamp, or prefix hash does not match stored metadata.
* `TRANSFER_NOT_FOUND`: Sender has no record or source path for this transfer.
* `PARTIAL_CORRUPT`: Receiver's partial prefix hash does not match source prefix.
* `WRONG_PEER`: Original `sender_identity` does not match the session's authenticated peer key.
* `STALE`: Transfer has exceeded retention TTL (7 days).
* `PEER_CANCELLED`: Transfer was cancelled by peer or user.

Any unknown reason code is rejected with `ValueError`.

---

## 3. `IncomingTransfer.prepare_resume_request()`

**File:** [`linux/src/ferry_linux/core/transfer.py`](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/transfer.py)

Prepares the receiver's resume request from local persistent state on disk:

```python
payload = incoming.prepare_resume_request(
    expected_bytes=...,       # Optional: cross-check with DB record
    expected_chunk_index=..., # Optional: cross-check with DB record
)
```

**Semantics & Preconditions:**
1. **State verification**: Transfer state must be `INTERRUPTED`. Calling from `IDLE`, `TRANSFERRING`, `COMPLETED`, `CANCELLED`, or `FAILED` raises `RuntimeError`.
2. **File presence**: The `.part` file must exist on disk. Missing `.part` raises `FileNotFoundError`.
3. **Non-zero file size**: A zero-byte partial file cannot be resumed and raises `ValueError`.
4. **Declared bounds check**: `disk_size` must not exceed `meta.file_size`.
5. **Safe boundary alignment**: `disk_size % meta.chunk_size == 0`. Unaligned files raise `ValueError`. The file is never silently truncated or padded.
6. **DB consistency cross-check**: If `expected_bytes` or `expected_chunk_index` are provided, any divergence from actual disk state raises `ValueError`.
7. **Local accounting sync**: Updates `self._bytes_received = disk_size` and `self._next_seq = resume_chunk_index`.
8. Returns a validated `TransferResumeRequestPayload`.

---

## 4. Disk-Authoritative Hashing

**Critical Rule:** The in-memory hash accumulator (`_hasher`) from the interrupted session is **NOT** used as the authoritative resume hash. If the process crashed or writes were buffered, in-memory state is untrustworthy or lost.

Instead, `prepare_resume_request()` opens the actual `.part` file from disk and computes its SHA-256 digest:
* **Bounded reads**: Uses 64 KiB read blocks (`buf_size = 65536`).
* **Memory safety**: Streams the file sequentially regardless of total size. Large partial files (e.g. 5 GiB) do not cause whole-file memory allocation.
* **Deterministic hex output**: Formatted as a 64-character lowercase hexadecimal string.

Unit test `test_14_prepare_resume_request_computes_sha256_from_disk` explicitly mutates disk data while keeping the in-memory hasher unchanged to prove that the disk content is strictly authoritative.

---

## 5. Offset / Chunk Semantics

The receiver derives resume parameters strictly from the validated on-disk `.part` file:
* `resume_offset_bytes = disk_size`
* `resume_chunk_index = disk_size // chunk_size`

### Safe Boundary Enforcement
* Full chunk boundaries (`disk_size % chunk_size == 0`): Valid resume point.
* Partial last chunk (`disk_size % chunk_size != 0`): Fails safely with `ValueError`. No silent truncation, no silent zero-padding.
* Zero-byte `.part` (`disk_size == 0`): Fails safely with `ValueError`.

---

## 6. Security Boundary

* **Session Authentication Invariant**: Resume messages are processed **only** within an established, authenticated Ferry session (`SessionState.ESTABLISHED`). No unauthenticated pre-handshake resume negotiation is permitted.
* **Non-Credential UUIDs**: The `transfer_id` is a lookup handle, not an authorization token. Possession of a `transfer_id` grants no access. Task 3 will enforce that the session peer's Ed25519 identity key matches the `sender_identity` recorded at transfer creation.
* **Replay Protection**: Resumed data frames inherit the fresh AEAD session keys and monotonic nonce counter of the new session. No AEAD key material or nonces are persisted across sessions.
* **Path Confinement**: `.part` files are addressed strictly by `staging/<transfer_id>.part` where `transfer_id` is a validated UUIDv4.

---

## 7. Backward Compatibility

* **Existing Message Types**: Handshake, pairing, and standard Phase 3A/3D transfer messages (`TRANSFER_REQUEST`, `TRANSFER_ACCEPT`, `TRANSFER_REJECT`, `TRANSFER_CHUNK`, `TRANSFER_CANCEL`, `TRANSFER_COMPLETE`, `TRANSFER_RESULT`, `TRANSFER_ERROR`) operate with zero behavioral changes.
* **Service-Level Dispatch**: Added minimal placeholder dispatch routes in `_handle_transfer_message` (`service.py`):
  * `_on_transfer_resume_request`
  * `_on_transfer_resume_accept`
  * `_on_transfer_resume_reject`
  These routes validate incoming payloads and log events without mutating transfer state prematurely or throwing unhandled-message warnings.
* **Wire Invariance**: Standard Ferry wire envelope framing is 100% preserved.

---

## 8. Tests Added & Results

### 8.1. `TestResumeProtocolModels` (`linux/tests/test_protocol_models.py`) — 11 Tests
* `test_01_transfer_resume_request_serialization`: Payload dict and FY binary framing.
* `test_02_transfer_resume_request_deserialization`: `from_dict()` reconstruction and dict indexing.
* `test_03_invalid_transfer_id`: Rejects non-UUID strings, empty strings.
* `test_04_negative_offset`: Rejects `resume_offset_bytes < 0`.
* `test_05_negative_chunk_index`: Rejects `resume_chunk_index < 0`.
* `test_06_invalid_sha256`: Rejects wrong length, non-hex, uppercase characters.
* `test_07_invalid_protocol_version`: Rejects versions != 1.
* `test_08_offset_chunk_mismatch`: Rejects inconsistent offset/chunk combinations.
* `test_09_transfer_resume_accept_serialization_and_validation`: Validates accept payload and rejects bad UUIDs/negative indexes.
* `test_10_transfer_resume_reject_serialization`: Validates reject serialization and round-trip.
* `test_11_invalid_reject_reason`: Rejects unknown reasons; validates all 6 allowed enum values.

### 8.2. `TestPhase3ETask2ResumeNegotiation` (`linux/tests/test_transfer.py`) — 10 Tests
* `test_12_prepare_resume_request_valid_part`: Validates resume payload for a 2-chunk `.part` file.
* `test_13_prepare_resume_request_uses_actual_disk_size`: Verifies offset/chunk derive from disk, ignoring stale in-memory counters.
* `test_14_prepare_resume_request_computes_sha256_from_disk`: Verifies SHA-256 is computed from disk, ignoring divergent in-memory hasher.
* `test_15_missing_part_file_raises`: Verifies `FileNotFoundError` when `.part` is missing.
* `test_16_invalid_non_interrupted_state_raises`: Verifies `RuntimeError` when called from IDLE, TRANSFERRING, or CANCELLED states.
* `test_17_partial_file_boundary_validation`: Verifies `ValueError` when `.part` is not chunk-aligned; verifies file is untouched.
* `test_18_large_part_streaming_without_whole_file_loading`: Verifies streaming with bounded `f.read(65536)` blocks.
* `test_19_zero_byte_partial_file_raises`: Verifies `ValueError` on 0-byte `.part` file.
* `test_20_inconsistent_db_metadata_raises`: Verifies DB metadata cross-check failure and success.
* `test_21_service_layer_resume_message_dispatch`: Verifies `service.py` cleanly dispatches all three resume message types.

### 8.3. Regression Verification
* **Pre-existing tests**: 183 / 183 passing.
* **New Task 2 tests**: 21 / 21 passing.
* **Total Linux tests**: **204 / 204 passing** (`0` failures, `0` errors, exit code `0`).
* **Android tests**: 57 / 57 passing (untouched).
* **Overall suite**: **261 tests passing**.

---

## 9. Known Limitations

> [!IMPORTANT]
> **Actual Reconnect / Resume Execution is NOT Yet Implemented:**
> * Session disconnects in `service.py` currently still trigger `cancel()` rather than `interrupt()`. Switching disconnect behavior to `interrupt()` for eligible transfers is part of Task 3.
> * Sender-side source prefix verification and chunk streaming from `resume_chunk_index` (`stream_chunks_from`) are not yet wired.
> * The `INTERRUPTED` state has no outgoing transitions defined in `TransferState.VALID_TRANSITIONS` yet.
> * No UI changes or Android resume features are included in this task.

---

## 10. Exact Next Task

**Phase 3E Task 3: Linux Resume Execution & Service Layer Integration**
* Update `TransferState.VALID_TRANSITIONS` to allow `INTERRUPTED → RESUME_REQUESTED → RESUMING`.
* Wire `service._cancel_incoming_transfer_for_peer()` to call `incoming.interrupt()` instead of `cancel()` when peer is known/trusted.
* Exempt `INTERRUPTED` `.part` files from startup cleanup.
* Implement sender-side `TRANSFER_RESUME_REQUEST` handler: verify peer identity against stored `sender_identity`, verify source file existence and prefix SHA-256, reply with `TRANSFER_RESUME_ACCEPT` or `TRANSFER_RESUME_REJECT`.
* Implement `OutgoingTransfer.stream_chunks_from(resume_chunk_index)`.
* Implement receiver-side `TRANSFER_RESUME_ACCEPT` handler: transition `IncomingTransfer` to `RESUMING` and resume chunk ingestion.
* Implement receiver-side `TRANSFER_RESUME_REJECT` handler: clean `.part` file and mark transfer `FAILED`.
