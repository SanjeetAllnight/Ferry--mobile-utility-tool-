# Phase 3E Task 1 — Persistent Interrupted Transfer Foundation

**Date:** 2026-09-05  
**Status:** COMPLETE  
**Scope:** DB schema v3 migration · INTERRUPTED state · `IncomingTransfer.interrupt()`

---

## 1. Deliverables

### 1.1. Database Schema Migration (v2 → v3)

**File:** [`linux/src/ferry_linux/core/db.py`](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/db.py)

`DB_SCHEMA_VERSION` bumped from `2` to `3`. The `_init_db()` method applies the migration under the existing `current_version < N` guard:

```sql
ALTER TABLE transfer_history ADD COLUMN interrupted_at        INTEGER DEFAULT NULL;
ALTER TABLE transfer_history ADD COLUMN bytes_received        INTEGER DEFAULT NULL;
ALTER TABLE transfer_history ADD COLUMN resume_chunk_index    INTEGER DEFAULT NULL;
ALTER TABLE transfer_history ADD COLUMN partial_sha256        TEXT    DEFAULT NULL;
ALTER TABLE transfer_history ADD COLUMN sender_identity       TEXT    DEFAULT NULL;
ALTER TABLE transfer_history ADD COLUMN original_metadata_json TEXT   DEFAULT NULL;
ALTER TABLE transfer_history ADD COLUMN expire_at             INTEGER DEFAULT NULL;
```

Each `ALTER TABLE` is wrapped in `try/except` — safe if the column already exists (idempotent). All existing rows receive `NULL` for all new columns and remain fully valid.

New **`InterruptedTransferInfo`** dataclass captures all resume state for a persisted interrupted transfer, including a `RESUME_TTL_MS = 7 days` constant and `make_expire_at()` helper.

New DB methods added to `DatabaseManager`:
- `save_interrupted_transfer(info)` — upserts resume state into `transfer_history`
- `get_interrupted_transfer(transfer_id)` — fetches single INTERRUPTED row
- `list_interrupted_transfers_for_peer(sender_identity)` — finds all resumable transfers from a given peer
- `expire_interrupted_transfers(now_ms)` — sets expired INTERRUPTED rows to FAILED

### 1.2. INTERRUPTED Transfer State

**File:** [`linux/src/ferry_linux/core/transfer.py`](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/transfer.py)

Added `INTERRUPTED = auto()` to the `TransferState` enum.

Added to `VALID_TRANSITIONS`:
- `TRANSFERRING → INTERRUPTED` (new; network disconnect mid-transfer)
- `INTERRUPTED → {}` (empty set; resume transitions are Phase 3E Task 2)

Existing transitions are completely unchanged. `CANCELLED`, `FAILED`, `COMPLETED` remain terminal with empty outgoing sets.

### 1.3. `IncomingTransfer.interrupt()`

**File:** [`linux/src/ferry_linux/core/transfer.py`](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/transfer.py)

Full method semantics:

| Scenario | Behavior |
|---|---|
| Called from `TRANSFERRING` (or `ACCEPTED`) | Closes file handle, reads disk size, transitions to `INTERRUPTED` |
| Called from `INTERRUPTED` | No-op (idempotent) |
| Called from `COMPLETED` | No-op (terminal state preserved) |
| Called from `FAILED` | No-op (terminal state preserved) |
| Called from `CANCELLED` | No-op (terminal state preserved) |
| Called from `IDLE` / `REQUESTED` | No .part file — transitions to `FAILED` |
| Zero-byte transfer (`file_size == 0`) | Cannot be resumed — transitions to `FAILED` |
| No data written (0 bytes on disk) | Nothing to resume — transitions to `FAILED` |

**Resume position derivation (critical):**  
The `_bytes_received` field is updated from `_temp_path.stat().st_size` — the actual on-disk size — not from in-memory counter alone. This prevents inconsistency if the OS buffered some writes that didn't reach disk.

`_next_seq` is recomputed from `actual_bytes_on_disk // chunk_size`. If the last chunk was partial (bytes_on_disk not a multiple of chunk_size), `_next_seq` rounds down to the last full chunk boundary. The partial chunk will need to be re-sent on resume — a safe, conservative approach.

**`interrupt_info()`** returns a `dict` suitable for constructing an `InterruptedTransferInfo`:
- `transfer_id`, `bytes_received`, `resume_chunk_index`
- `partial_sha256` (from the running SHA-256 accumulator — see Known Limitations)
- `sender_identity`, `original_metadata_json` (from `meta`)

Returns `None` if state is not `INTERRUPTED`.

---

## 2. Cancellation/Failure Separation

The following behaviors are **completely unchanged** from Phase 3D:

- **`cancel()`**: always deletes the `.part` file and transitions to `CANCELLED`.
- **`finalise()` on integrity failure**: deletes `.part`, transitions to `FAILED`.
- **`receive_chunk()` on disk-full**: deletes `.part`, transitions to `FAILED`.
- **`begin()` on IO error**: transitions to `FAILED`.
- Zero-byte completed transfers: unaffected; `finalise()` path unchanged.

The only new behavior is that a **genuine session disconnect** can now call `interrupt()` instead of `cancel()`, preserving the `.part` file. This is an *optional* path — the service layer decides whether to call `interrupt()` or `cancel()` (service integration is Phase 3E Task 3).

---

## 3. Persistence Semantics

The `transfer_history` row for an interrupted transfer has `status = 'INTERRUPTED'` with:
- `bytes_received` = exact bytes on disk at time of interrupt
- `resume_chunk_index` = `bytes_received // chunk_size` (full chunk boundary)
- `partial_sha256` = SHA-256 of bytes fed to the running hasher (see Known Limitations)
- `sender_identity` = Ed25519 public key of sender (for peer binding on resume)
- `original_metadata_json` = full `TransferMetadata.to_dict()` JSON blob
- `interrupted_at` = epoch-ms of interrupt
- `expire_at` = `interrupted_at + 7 days`

On service startup, `expire_interrupted_transfers()` should be called to clean up stale rows. The callers must also delete the corresponding `.part` files from disk (not done automatically by the DB method).

---

## 4. Known Limitations

### Partial SHA-256 Limitation (by design)

`interrupt_info()['partial_sha256']` is derived from the in-session SHA-256 accumulator (`_hasher`). This is correct when:
- The process did not crash (the hasher state is in memory from the current session).
- All writes to `_temp_fh` succeeded (no partial write lost).

It is **incorrect** if:
- The process crashes mid-session (hasher state is lost).
- A write partially failed in a way that left disk bytes inconsistent with the hasher.

**Mitigation (Task 2):** In Phase 3E Task 2, when the receiver sends `TRANSFER_RESUME_REQUEST`, it must re-compute the partial SHA-256 by reading the `.part` file from disk (not from `interrupt_info()['partial_sha256']`). The in-session value from `interrupt_info()` is an optimization for sessions where the process did not crash — it should be verified or discarded by the Task 2 resume negotiation layer.

### Service Layer Not Yet Wired

`interrupt()` exists on `IncomingTransfer` but `service.py` does not call it yet. The existing service-layer session-disconnect handler calls `cancel()`. Wiring `interrupt()` into the service layer is Phase 3E Task 3.

### `INTERRUPTED → RESUME_REQUESTED` Transition Not Yet Defined

`TransferState.VALID_TRANSITIONS[INTERRUPTED]` is an empty set. The resume path will add `INTERRUPTED → RESUME_REQUESTED → RESUMING` in Task 2.

### No Automatic Cleanup of Orphaned `.part` Files

The existing Phase 3D stale-`.part` cleanup (on service startup) deletes **all** `.part` files in the staging directory that do not have an active in-memory transfer. With Phase 3E, `.part` files for `INTERRUPTED` transfers must be **exempt** from this cleanup. This change to `service.py` is part of Task 3.

---

## 5. Tests Added

22 new tests across 2 test classes in [`linux/tests/test_transfer.py`](file:///home/sanjeet/Projects/Ferry/linux/tests/test_transfer.py).

### `TestPhase3ETask1InterruptedTransfer` (15 tests)

| ID | Description |
|---|---|
| E01 | `interrupt()` from `TRANSFERRING` → `INTERRUPTED` |
| E02 | `.part` file exists on disk after `interrupt()` |
| E03 | `bytes_received` equals actual `.part` file size on disk |
| E04 | `resume_chunk_index` correct for full-chunk data |
| E05 | File handle is `None` (closed) after `interrupt()` |
| E06 | `interrupt()` is idempotent (second call no-ops) |
| E07 | Metadata accessible after `interrupt()` |
| E08 | `interrupt_info()` returns all required keys with correct values |
| E09 | `interrupt()` on `COMPLETED` is a no-op |
| E10 | `interrupt()` on `FAILED` does not resurrect the transfer |
| E11 | `interrupt()` on `CANCELLED` does not resurrect the transfer |
| E12 | `interrupt_info()` returns `None` when state is not `INTERRUPTED` |
| E13 | Zero-byte transfer → `FAILED`, not `INTERRUPTED` |
| E14 | `TRANSFERRING → INTERRUPTED` is a valid state machine transition |
| E15 | `INTERRUPTED` has an empty valid-transitions set in Task 1 |

### `TestPhase3ETask1DatabaseMigration` (7 tests)

| ID | Description |
|---|---|
| DB01 | Schema v2 → v3 migration adds all 7 new columns |
| DB02 | Existing `transfer_history` rows survive migration intact |
| DB03 | Migration is idempotent (v3 → v3 is safe) |
| DB04 | Old rows have `NULL` for all new resume columns |
| DB05 | `save_interrupted_transfer` + `get_interrupted_transfer` round-trip |
| DB06 | `get_interrupted_transfer` returns `None` for `COMPLETED` transfers |
| DB07 | `expire_interrupted_transfers()` marks expired rows as `FAILED` |

---

## 6. Test Results

```
Ran 183 tests in 11.779s

OK
```

- **161** pre-existing tests: all passing (no regressions)
- **22** new Phase 3E Task 1 tests: all passing
- **Total: 183 / 183 passing**

---

## 7. Files Changed

| File | Change |
|---|---|
| [`linux/src/ferry_linux/core/db.py`](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/db.py) | Schema v3, `InterruptedTransferInfo`, 4 new DB methods |
| [`linux/src/ferry_linux/core/transfer.py`](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/transfer.py) | `INTERRUPTED` state, `interrupt()`, `interrupt_info()` |
| [`linux/tests/test_transfer.py`](file:///home/sanjeet/Projects/Ferry/linux/tests/test_transfer.py) | 22 new tests in 2 classes |

No Android code was modified. No service.py was modified. No existing behavior was changed.

---

## 8. Exact Next Task

**Phase 3E Task 2: Resume Negotiation Protocol**

Implement:
1. `TRANSFER_RESUME_REQUEST`, `TRANSFER_RESUME_ACCEPT`, `TRANSFER_RESUME_REJECT` in `protocol/models.py`.
2. `IncomingTransfer.prepare_resume_request()` — reads `.part` file, computes prefix SHA-256, returns payload.
3. Service layer: on session ESTABLISHED, call `db.list_interrupted_transfers_for_peer(peer_identity)` and emit to UI.
4. Service layer: handle `TRANSFER_RESUME_REQUEST` from peer (when Linux is sender), verify source file prefix hash, send ACCEPT or REJECT.
5. Service layer: handle `TRANSFER_RESUME_ACCEPT` (when Linux is receiver), resume streaming.

**Pre-condition:** Task 1 must be complete. ✅

**Success criteria for Task 2:**
- `TRANSFER_RESUME_REQUEST` sent by receiver over an authenticated session.
- Sender verifies prefix hash and sends ACCEPT or REJECT.
- On ACCEPT, receiver resumes streaming from `resume_chunk_index`.
- On REJECT, receiver deletes `.part`, transitions to `FAILED`.
- Full end-to-end interrupt → reconnect → resume → `COMPLETED` cycle passes in integration tests.
- All 183 existing tests continue to pass.
