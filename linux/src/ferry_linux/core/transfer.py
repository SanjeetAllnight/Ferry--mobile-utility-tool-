"""
Ferry Transfer Layer — Phase 3A / Phase 3D reliability hardening / Phase 3E Tasks 1-3.

Implements the reusable secure data transport for file transfers over an already-
authenticated Ferry control session (ESTABLISHED state).

Architecture: In-band multiplexing over the existing ChaCha20-Poly1305 encrypted
TCP session (ADR 007). Transfer data chunks are delivered as TRANSFER_CHUNK AEAD
frames whose decrypted plaintext starts with a binary FYCH header rather than a
JSON envelope, distinguishing them from control messages without fragile type-string
parsing.

Control messages (TRANSFER_REQUEST, TRANSFER_ACCEPT, TRANSFER_REJECT,
TRANSFER_CANCEL, TRANSFER_COMPLETE, TRANSFER_RESULT, TRANSFER_ERROR) use the
standard FerryEnvelope JSON structure.

Data message (TRANSFER_CHUNK) binary layout (inside the AEAD-decrypted plaintext):
    [4B magic "FYCH"] [16B transfer_id as UUID bytes] [4B seq_number big-endian uint32]
    [4B payload_len big-endian uint32] [N bytes raw chunk data]

Security:
- All frames inherit authentication from the AEAD session.
- Only sessions in ESTABLISHED state may initiate or receive transfers.
- File names are sanitised to basename-only before any filesystem use.
- Temp files use transfer_id as a suffix; only renamed after SHA-256 passes.
- Remote-supplied lengths are validated before allocation.
- No private key material is used here; trust is inherited from Phase 2 auth.

Phase 3D reliability additions:
- TransferError exception for filesystem/IO errors during transfers.
- IncomingTransfer.begin/receive_chunk/finalise: all OS errors are caught, temp
  file is cleaned up, and state transitions to FAILED before re-raising.
- IncomingTransfer.cancel(): safe to call from any state.
- OutgoingTransfer.stream_chunks(): guards file-open and reads; raises TransferError
  on IOError so the caller can send TRANSFER_CANCEL and record a FAILED history row.

Phase 3E Task 1 additions:
- INTERRUPTED transfer state: network disconnect mid-transfer preserves the .part
  file for a future resume (rather than deleting it).
- IncomingTransfer.interrupt(): safely closes the file handle and transitions the
  state to INTERRUPTED, recording the next expected chunk index and bytes received.
  The .part file is NOT deleted; it is retained for a later resume attempt.

Phase 3E Task 3 additions:
- RESUME_REQUESTED and RESUMING states added to the transfer state machine.
- IncomingTransfer.resume(chunk_index): re-opens the .part file in append mode,
  initialises _next_seq from the persisted chunk index, resets the running SHA-256
  accumulator to reflect bytes already received, and transitions to RESUMING.
- OutgoingTransfer.stream_chunks_from(resume_chunk_index): seeks the source file
  to resume_chunk_index * chunk_size and streams from that offset, with seq numbers
  starting at resume_chunk_index. No change to the FYCH binary format.

CRITICAL: Do not change the AEAD session keys or nonce counter in this module.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from ..protocol.models import TransferResumeRequestPayload

logger = logging.getLogger("ferry.transfer")

# ─── Constants ────────────────────────────────────────────────────────────────

CHUNK_MAGIC = b"FYCH"               # 4 bytes — identifies a TRANSFER_CHUNK binary frame
CHUNK_MAGIC_SIZE = 4
CHUNK_TRANSFER_ID_SIZE = 16         # UUID bytes (no hyphens)
CHUNK_SEQ_SIZE = 4                  # uint32 big-endian
CHUNK_LEN_SIZE = 4                  # uint32 big-endian
CHUNK_HEADER_SIZE = CHUNK_MAGIC_SIZE + CHUNK_TRANSFER_ID_SIZE + CHUNK_SEQ_SIZE + CHUNK_LEN_SIZE  # 28

CHUNK_SIZE = 65536                  # 64 KiB — selected chunk payload size
MAX_CHUNK_PAYLOAD = CHUNK_SIZE      # remote-supplied len must not exceed this
MAX_FILE_NAME_BYTES = 255           # enforced before any filesystem operation
MAX_TRANSFER_METADATA_BYTES = 4096  # JSON metadata payload size limit

# Phase 3A MVP: single active transfer at a time
MAX_CONCURRENT_TRANSFERS = 1

# Idle timeout for stalled transfers (no chunk received within this window)
TRANSFER_CHUNK_TIMEOUT_SECS = 120.0

# Phase 3D: Timeout (seconds) for user to accept/reject an incoming transfer request
TRANSFER_ACCEPT_TIMEOUT_SECS = 120.0


# ─── Transfer State Machine ────────────────────────────────────────────────────

class TransferState(Enum):
    """Explicit state machine for one file transfer."""
    IDLE = auto()
    REQUESTED = auto()          # TRANSFER_REQUEST sent/received; awaiting decision
    ACCEPTED = auto()           # TRANSFER_ACCEPT exchanged; ready to stream
    TRANSFERRING = auto()       # Chunks flowing
    CANCELLING = auto()         # TRANSFER_CANCEL sent; draining
    COMPLETED = auto()          # SHA-256 verified and file finalised
    FAILED = auto()             # Error (IO, integrity, protocol)
    CANCELLED = auto()          # Cancelled by either side
    INTERRUPTED = auto()        # Phase 3E: network disconnect mid-transfer; .part retained
    RESUME_REQUESTED = auto()   # Phase 3E: receiver sent TRANSFER_RESUME_REQUEST; awaiting sender decision
    RESUMING = auto()           # Phase 3E: resume accepted; chunks flowing from resume_chunk_index

    VALID_TRANSITIONS: dict  # defined below

# Define valid state transitions after the class body
TransferState.VALID_TRANSITIONS = {
    TransferState.IDLE:             {TransferState.REQUESTED, TransferState.ACCEPTED,
                                     TransferState.TRANSFERRING, TransferState.FAILED,
                                     TransferState.CANCELLED},
    TransferState.REQUESTED:        {TransferState.ACCEPTED, TransferState.FAILED,
                                     TransferState.CANCELLED},
    TransferState.ACCEPTED:         {TransferState.TRANSFERRING, TransferState.FAILED,
                                     TransferState.CANCELLED},
    TransferState.TRANSFERRING:     {TransferState.CANCELLING, TransferState.COMPLETED,
                                     TransferState.FAILED, TransferState.INTERRUPTED},
    TransferState.CANCELLING:       {TransferState.CANCELLED, TransferState.FAILED},
    TransferState.COMPLETED:        set(),
    TransferState.FAILED:           set(),
    TransferState.CANCELLED:        set(),
    # INTERRUPTED → RESUME_REQUESTED (user taps Resume) or FAILED (user discards / expiry)
    TransferState.INTERRUPTED:      {TransferState.RESUME_REQUESTED, TransferState.FAILED},
    # RESUME_REQUESTED → RESUMING (ACCEPT) or FAILED (REJECT / timeout)
    TransferState.RESUME_REQUESTED: {TransferState.RESUMING, TransferState.FAILED},
    # RESUMING → COMPLETED (all chunks done), INTERRUPTED (disconnect again), FAILED (IO error)
    TransferState.RESUMING:         {TransferState.COMPLETED, TransferState.INTERRUPTED,
                                     TransferState.FAILED},
}


def _transfer_transition(current: TransferState, new: TransferState) -> TransferState:
    allowed = TransferState.VALID_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise RuntimeError(
            f"Invalid transfer state transition: {current.name} → {new.name}"
        )
    return new

class BatchState(Enum):
    REQUESTED = auto()
    ACCEPTED = auto()
    TRANSFERRING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()
    PARTIAL = auto()

# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class TransferMetadata:
    """
    Metadata for one file transfer, derived from TRANSFER_REQUEST payload.
    All fields are validated before use; no raw remote values reach the filesystem.
    """
    transfer_id: str         # UUIDv4 string
    file_name: str           # basename only — see sanitise_filename()
    file_size: int           # bytes; must be > 0
    mime_type: str           # e.g. "image/jpeg"
    sha256: str              # hex-encoded SHA-256 of the complete file
    chunk_size: int          # negotiated chunk size; must be ≤ MAX_CHUNK_PAYLOAD
    chunk_count: int         # ceil(file_size / chunk_size)
    sender_identity: str     # sender's Ed25519 public key (base64url)
    created_at: int          # ms since epoch
    protocol_version: int = 1
    batch_id: str = ""       # If non-empty, transfer belongs to this batch
    relative_path: str = ""  # If non-empty, relative directory path within the batch

    @classmethod
    def from_dict(cls, data: dict) -> "TransferMetadata":
        """Parse and validate a TRANSFER_REQUEST payload dict."""
        transfer_id = str(data["transfer_id"])
        # Validate UUID format
        uuid.UUID(transfer_id)

        raw_name = str(data.get("file_name", ""))
        file_name = sanitise_filename(raw_name)
        if not file_name:
            raise ValueError(f"Empty or invalid file_name after sanitisation: {raw_name!r}")

        # Phase 4C: Extract and strictly validate relative_path
        relative_path = str(data.get("relative_path", ""))
        if relative_path:
            import os
            norm_path = os.path.normpath(relative_path)
            if norm_path.startswith("/") or norm_path.startswith("\\") or norm_path.startswith("..") or "/../" in norm_path or "\\..\\" in norm_path:
                raise ValueError(f"Path traversal detected in relative_path: {relative_path!r}")
            relative_path = norm_path

        file_size = int(data["file_size"])
        if file_size < 0:
            raise ValueError(f"file_size must be non-negative, got {file_size}")
        if file_size > 10 * 1024 ** 3:  # 10 GiB soft limit
            raise ValueError(f"file_size {file_size} exceeds 10 GiB limit")

        chunk_size = int(data.get("chunk_size", CHUNK_SIZE))
        if chunk_size <= 0 or chunk_size > MAX_CHUNK_PAYLOAD:
            raise ValueError(f"chunk_size {chunk_size} out of range (1..{MAX_CHUNK_PAYLOAD})")

        chunk_count = int(data.get("chunk_count", -1))
        expected_chunks = (file_size + chunk_size - 1) // chunk_size if file_size > 0 else 0
        if chunk_count != expected_chunks:
            raise ValueError(
                f"chunk_count {chunk_count} does not match file_size/chunk_size "
                f"(expected {expected_chunks})"
            )

        sha256 = str(data.get("sha256", ""))
        if len(sha256) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sha256):
            raise ValueError(f"sha256 field is not a valid 64-char hex string: {sha256!r}")

        return cls(
            transfer_id=transfer_id,
            file_name=file_name,
            file_size=file_size,
            mime_type=str(data.get("mime_type", "application/octet-stream")),
            sha256=sha256.lower(),
            chunk_size=chunk_size,
            chunk_count=chunk_count,
            sender_identity=str(data.get("sender_identity", "")),
            created_at=int(data.get("created_at", int(time.time() * 1000))),
            protocol_version=int(data.get("protocol_version", 1)),
            batch_id=str(data.get("batch_id", "")),
            relative_path=relative_path,
        )

    def to_dict(self) -> dict:
        return {
            "transfer_id": self.transfer_id,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "sha256": self.sha256,
            "chunk_size": self.chunk_size,
            "chunk_count": self.chunk_count,
            "sender_identity": self.sender_identity,
            "created_at": self.created_at,
            "protocol_version": self.protocol_version,
            "batch_id": self.batch_id,
            "relative_path": self.relative_path,
        }


@dataclass
class ChunkFrame:
    """Decoded binary TRANSFER_CHUNK frame."""
    transfer_id: str    # UUIDv4 string
    seq: int            # chunk sequence number (0-based)
    data: bytes         # raw chunk payload


# ─── Binary chunk framing ──────────────────────────────────────────────────────

def encode_chunk_frame(transfer_id: str, seq: int, data: bytes) -> bytes:
    """
    Encode a TRANSFER_CHUNK binary frame.

    Layout (total = 28 + len(data)):
        [4B "FYCH"] [16B UUID bytes] [4B seq uint32 BE] [4B payload_len uint32 BE] [data]
    """
    if len(data) > MAX_CHUNK_PAYLOAD:
        raise ValueError(f"Chunk payload {len(data)} exceeds MAX_CHUNK_PAYLOAD {MAX_CHUNK_PAYLOAD}")
    uid_bytes = uuid.UUID(transfer_id).bytes  # 16 bytes, no hyphens
    header = (
        CHUNK_MAGIC
        + uid_bytes
        + struct.pack("!I", seq)
        + struct.pack("!I", len(data))
    )
    return header + data


def decode_chunk_frame(plaintext: bytes) -> ChunkFrame:
    """
    Decode a TRANSFER_CHUNK binary plaintext (already AEAD-decrypted).

    Raises ValueError on any structural or bounds violation.
    """
    if len(plaintext) < CHUNK_HEADER_SIZE:
        raise ValueError(
            f"TRANSFER_CHUNK too short: {len(plaintext)} < {CHUNK_HEADER_SIZE}"
        )
    magic = plaintext[:CHUNK_MAGIC_SIZE]
    if magic != CHUNK_MAGIC:
        raise ValueError(f"Bad TRANSFER_CHUNK magic: {magic!r}")

    uid_bytes = plaintext[CHUNK_MAGIC_SIZE: CHUNK_MAGIC_SIZE + CHUNK_TRANSFER_ID_SIZE]
    transfer_id = str(uuid.UUID(bytes=uid_bytes))

    offset = CHUNK_MAGIC_SIZE + CHUNK_TRANSFER_ID_SIZE
    (seq,) = struct.unpack("!I", plaintext[offset: offset + 4])
    offset += 4
    (payload_len,) = struct.unpack("!I", plaintext[offset: offset + 4])
    offset += 4

    if payload_len > MAX_CHUNK_PAYLOAD:
        raise ValueError(
            f"Remote chunk payload_len {payload_len} > MAX_CHUNK_PAYLOAD {MAX_CHUNK_PAYLOAD}"
        )
    if len(plaintext) < offset + payload_len:
        raise ValueError(
            f"TRANSFER_CHUNK truncated: need {offset + payload_len}, have {len(plaintext)}"
        )

    data = plaintext[offset: offset + payload_len]
    return ChunkFrame(transfer_id=transfer_id, seq=seq, data=data)


def is_chunk_frame(plaintext: bytes) -> bool:
    """Return True if this plaintext begins with the FYCH magic (data frame, not JSON control)."""
    return plaintext[:CHUNK_MAGIC_SIZE] == CHUNK_MAGIC


# ─── Filename safety ──────────────────────────────────────────────────────────

# Windows/DOS reserved names that must never reach the filesystem
_RESERVED_NAMES = frozenset([
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
])


def sanitise_filename(raw: str) -> str:
    """
    Sanitise a remote-supplied filename to a safe basename.

    Strips all path components, rejects control characters, truncates to
    MAX_FILE_NAME_BYTES, and rejects reserved names.  Returns an empty string
    if the name cannot be made safe.
    """
    # Extract basename only — prevent any path traversal
    name = os.path.basename(raw.replace("\\", "/").replace("\x00", ""))
    # Strip dots/slashes that survived
    name = name.lstrip("./")
    # Remove control characters (ASCII 0-31, 127)
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)
    # Truncate to max bytes (preserve valid UTF-8 boundary)
    encoded = name.encode("utf-8")[:MAX_FILE_NAME_BYTES]
    name = encoded.decode("utf-8", errors="ignore")
    # Reject reserved names (case-insensitive, with or without extension)
    stem = name.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        return ""
    # Must not be empty or a dot-only name
    if not name or name in (".", ".."):
        return ""
    return name


def resolve_safe_destination(staging_dir: Path, file_name: str, relative_path: str = "") -> Path:
    """
    Resolve the safe staging destination for a received file.
    If relative_path is provided, it is used to reconstruct the directory structure.

    Raises SecurityError if the resolved path escapes staging_dir.
    """
    sanitised = sanitise_filename(file_name)
    if not sanitised:
        raise ValueError(f"Cannot resolve safe destination for {file_name!r}")
        
    if relative_path:
        norm_rel = os.path.normpath(relative_path)
        if norm_rel.startswith("/") or norm_rel.startswith("\\") or norm_rel.startswith("..") or "/../" in norm_rel or "\\..\\" in norm_rel:
            raise SecurityError(f"Path traversal detected in relative_path {relative_path!r}")
        # Build destination with relative path
        dest = (staging_dir / norm_rel).resolve()
    else:
        dest = (staging_dir / sanitised).resolve()
        
    staging_resolved = staging_dir.resolve()
    if not str(dest).startswith(str(staging_resolved) + os.sep) and dest != staging_resolved:
        raise SecurityError(f"Path traversal detected for {file_name!r} → {dest}")
    return dest


class SecurityError(Exception):
    """Raised when a security constraint is violated."""


class TransferError(Exception):
    """
    Raised when a filesystem or I/O error occurs during an active transfer.

    Distinct from SecurityError (which covers path-traversal attacks) and
    ValueError (which covers protocol/framing errors).  Callers should:
      - Clean up the .part file (already done by the object that raises).
      - Transition the transfer state to FAILED (already done).
      - Send TRANSFER_ERROR or TRANSFER_CANCEL to the peer.
      - Record a FAILED entry in the transfer history DB.
    """


# ─── Incoming transfer session ────────────────────────────────────────────────

class IncomingTransfer:
    """
    Manages one incoming file transfer from a remote sender.

    Streams chunk data to a temporary file.  Final SHA-256 is verified before
    the temp file is atomically renamed to its final destination.
    """

    def __init__(self, meta: TransferMetadata, download_dir: Path) -> None:
        self.meta = meta
        self._download_dir = download_dir
        self._staging_dir = download_dir / "staging"
        self._state = TransferState.IDLE
        self._temp_path: Optional[Path] = None
        self._final_path: Optional[Path] = None
        self._hasher = hashlib.sha256()
        self._bytes_received: int = 0
        self._next_seq: int = 0
        self._temp_fh = None

    @property
    def state(self) -> TransferState:
        return self._state

    @property
    def bytes_received(self) -> int:
        return self._bytes_received

    def begin(self) -> None:
        """Open the temp file and transition to ACCEPTED then TRANSFERRING.

        Raises TransferError if the staging directory cannot be created or the
        temporary file cannot be opened.  On error, the state transitions to
        FAILED and no .part file is left open.
        """
        self._state = _transfer_transition(self._state, TransferState.ACCEPTED)
        staging_dir = self._staging_dir
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            self._download_dir.mkdir(parents=True, exist_ok=True)
            temp_name = f"{self.meta.transfer_id}.part"
            self._temp_path = staging_dir / temp_name
            self._final_path = resolve_safe_destination(self._download_dir, self.meta.file_name, self.meta.relative_path)
            
            # Phase 4C: create parent directories if relative_path was used
            self._final_path.parent.mkdir(parents=True, exist_ok=True)
            
            self._temp_fh = open(self._temp_path, "wb")
        except OSError as exc:
            logger.error(
                "Cannot open staging file for transfer %s: %s",
                self.meta.transfer_id[:8], exc,
            )
            self._state = _transfer_transition(self._state, TransferState.FAILED)
            raise TransferError(
                f"Failed to open staging file for {self.meta.transfer_id[:8]}: {exc}"
            ) from exc
        self._state = _transfer_transition(self._state, TransferState.TRANSFERRING)
        logger.info(
            "Incoming transfer %s started: %s (%d bytes, %d chunks)",
            self.meta.transfer_id[:8], self.meta.file_name,
            self.meta.file_size, self.meta.chunk_count,
        )

    def receive_chunk(self, frame: ChunkFrame) -> None:
        """
        Process one received chunk.

        Raises ValueError on sequence mismatch or oversized payload.
        Raises RuntimeError if not in TRANSFERRING or RESUMING state.
        Raises TransferError on disk I/O failure (disk full, permission denied, etc.);
          the temp file is cleaned and state is set to FAILED before raising.
        """
        if self._state not in (TransferState.TRANSFERRING, TransferState.RESUMING):
            raise RuntimeError(f"Cannot receive chunk in state {self._state.name}")
        if frame.transfer_id != self.meta.transfer_id:
            raise ValueError(
                f"Chunk transfer_id mismatch: expected {self.meta.transfer_id}, "
                f"got {frame.transfer_id}"
            )
        if frame.seq != self._next_seq:
            raise ValueError(
                f"Out-of-order chunk: expected seq {self._next_seq}, got {frame.seq}"
            )
        if len(frame.data) > self.meta.chunk_size:
            raise ValueError(
                f"Chunk payload {len(frame.data)} > declared chunk_size {self.meta.chunk_size}"
            )

        try:
            self._temp_fh.write(frame.data)
        except OSError as exc:
            logger.error(
                "Disk I/O error writing chunk seq=%d for transfer %s: %s",
                frame.seq, self.meta.transfer_id[:8], exc,
            )
            self._cleanup_on_io_error()
            raise TransferError(
                f"Disk write failed for transfer {self.meta.transfer_id[:8]}: {exc}"
            ) from exc
        self._hasher.update(frame.data)
        self._bytes_received += len(frame.data)
        self._next_seq += 1

    def finalise(self) -> bool:
        """
        Verify SHA-256 and atomically rename temp file to final destination.

        Returns True if integrity check passes, False otherwise.
        Deletes the temp file on failure.
        """
        if self._temp_fh:
            self._temp_fh.flush()
            self._temp_fh.close()
            self._temp_fh = None

        actual = self._hasher.hexdigest()
        expected = self.meta.sha256.lower()

        if actual != expected:
            logger.error(
                "Integrity mismatch for transfer %s: expected %s, got %s",
                self.meta.transfer_id[:8], expected, actual,
            )
            self._cleanup_temp()
            self._state = _transfer_transition(self._state, TransferState.FAILED)
            return False

        # Atomic rename
        assert self._temp_path is not None
        assert self._final_path is not None
        # Handle filename conflicts: append transfer_id prefix if needed
        dest = self._final_path
        if dest.exists():
            dest = dest.parent / f"{self.meta.transfer_id[:8]}_{dest.name}"
        self._temp_path.rename(dest)
        self._state = _transfer_transition(self._state, TransferState.COMPLETED)
        logger.info(
            "Transfer %s complete → %s (SHA-256 verified)",
            self.meta.transfer_id[:8], dest,
        )
        return True

    def interrupt(self) -> None:
        """
        Phase 3E Task 1 — Interrupt a mid-flight incoming transfer.

        Semantics:
        - Safely closes the open file handle (flushing OS-level buffers).
        - Does NOT delete the .part file — it is retained for a future resume.
        - Sets state to INTERRUPTED.
        - Records bytes_received and resume_chunk_index from trusted local state.
          These values are derived from what has actually been written to disk,
          NOT from any remote-supplied field.
        - Is idempotent: calling interrupt() more than once on an already-INTERRUPTED
          transfer has no effect and does not raise.
        - Does NOT transition CANCELLED, FAILED, or COMPLETED transfers — those
          terminal states are preserved unchanged.

        Resumability constraints:
        - Zero-byte transfers (file_size == 0) are not made resumable; this method
          transitions them to FAILED instead of INTERRUPTED so that the existing
          zero-byte handling path remains unchanged.
        - A transfer that has received no data at all (bytes_received == 0 and
          no .part file) transitions to FAILED rather than INTERRUPTED, since
          there is nothing to resume from.

        After interrupt(), callers should call db.save_interrupted_transfer() with
        the interrupt_info() snapshot to persist the resume state.
        """
        # Already in a terminal or interrupted state — do nothing.
        if self._state in (
            TransferState.INTERRUPTED,
            TransferState.COMPLETED,
            TransferState.FAILED,
            TransferState.CANCELLED,
        ):
            return

        # Only TRANSFERRING (and technically ACCEPTED) can be interrupted mid-flight.
        # Any other in-progress state (IDLE, REQUESTED) has no .part file to preserve.
        if self._state not in (TransferState.TRANSFERRING, TransferState.ACCEPTED):
            # Nothing to preserve — treat as a failure.
            self._state = TransferState.FAILED
            return

        # Zero-byte transfers cannot be resumed — treat as failure.
        if self.meta.file_size == 0:
            self._state = TransferState.FAILED
            return

        # Close the file handle safely, flushing buffered data to the OS.
        if self._temp_fh is not None:
            try:
                self._temp_fh.flush()
                self._temp_fh.close()
            except Exception as exc:
                logger.warning(
                    "Could not flush/close temp file for interrupted transfer %s: %s",
                    self.meta.transfer_id[:8], exc,
                )
            finally:
                self._temp_fh = None

        # Verify the .part file exists and measure actual written bytes.
        # bytes_received is already tracked per-chunk; use it as the primary source.
        # Cross-check against the file size on disk for safety.
        actual_bytes_on_disk: int = 0
        if self._temp_path is not None and self._temp_path.exists():
            try:
                actual_bytes_on_disk = self._temp_path.stat().st_size
            except OSError:
                actual_bytes_on_disk = 0

        if actual_bytes_on_disk == 0 and self._bytes_received == 0:
            # No data written — nothing to resume from.
            logger.debug(
                "Interrupt on transfer %s with no data written; treating as FAILED",
                self.meta.transfer_id[:8],
            )
            self._state = TransferState.FAILED
            return

        # Use the disk-measured size as the authoritative resume offset.
        # This guards against a scenario where _bytes_received was incremented
        # but the write failed before data hit the OS page cache.
        self._bytes_received = actual_bytes_on_disk
        # Recompute resume_chunk_index from the actual byte count.
        # For full-chunk boundaries: chunk_index = bytes_on_disk // chunk_size.
        # For a partial last chunk: we cannot resume mid-chunk safely without
        # re-reading, so we record the last fully-received chunk boundary.
        # This may require re-sending the last partial chunk, but is safe.
        if self.meta.chunk_size > 0:
            self._next_seq = actual_bytes_on_disk // self.meta.chunk_size
        # If actual_bytes_on_disk is not a multiple of chunk_size, the last
        # partial chunk must be discarded and re-sent — record the lower bound.
        # _next_seq now holds the index of the first chunk that needs re-sending.

        self._state = _transfer_transition(self._state, TransferState.INTERRUPTED)
        logger.info(
            "Incoming transfer %s INTERRUPTED at %d bytes (%d chunks); .part file retained at %s",
            self.meta.transfer_id[:8],
            self._bytes_received,
            self._next_seq,
            self._temp_path,
        )

    def interrupt_info(self) -> Optional[dict]:
        """
        Return a snapshot of the interrupt state suitable for passing to
        db.save_interrupted_transfer().

        Returns None if the transfer is not in INTERRUPTED state.

        The caller should construct an InterruptedTransferInfo from this dict:

            info_dict = transfer.interrupt_info()
            if info_dict:
                now_ms = int(time.time() * 1000)
                info = InterruptedTransferInfo(
                    transfer_id=info_dict["transfer_id"],
                    bytes_received=info_dict["bytes_received"],
                    resume_chunk_index=info_dict["resume_chunk_index"],
                    partial_sha256=info_dict["partial_sha256"],
                    sender_identity=info_dict["sender_identity"],
                    original_metadata_json=info_dict["original_metadata_json"],
                    interrupted_at=now_ms,
                    expire_at=InterruptedTransferInfo.make_expire_at(now_ms),
                )
                db.save_interrupted_transfer(info)

        NOTE: partial_sha256 is the SHA-256 of bytes accumulated so far in the
        running hasher. This matches what has been fed to the hasher, which should
        equal what is on disk for full chunks.

        IMPORTANT LIMITATION: The partial_sha256 stored here is computed from the
        running hashlib accumulator, which reflects bytes fed to the hasher during
        this session. If the process crashed mid-session, the hasher state is lost.
        In Phase 3E Task 2 (resume negotiation), the partial_sha256 must be
        re-computed from the .part file on disk before sending TRANSFER_RESUME_REQUEST.
        This method provides the in-session value for convenience.
        """
        if self._state != TransferState.INTERRUPTED:
            return None
        return {
            "transfer_id": self.meta.transfer_id,
            "bytes_received": self._bytes_received,
            "resume_chunk_index": self._next_seq,
            "partial_sha256": self._hasher.hexdigest(),
            "sender_identity": self.meta.sender_identity,
            "original_metadata_json": __import__("json").dumps(self.meta.to_dict()),
        }

    def prepare_resume_request(
        self,
        expected_bytes: Optional[int] = None,
        expected_chunk_index: Optional[int] = None,
    ) -> TransferResumeRequestPayload:
        """
        Phase 3E Task 2 — Prepare a resume request from the actual .part file on disk.

        Preconditions & Invariants:
        - The transfer must be in INTERRUPTED state.
        - The .part file must exist on disk.
        - The .part file size must be non-zero and aligned to the chunk boundary
          (meta.chunk_size). No partial chunks are silently truncated or padded.
        - The .part file size must not exceed the expected file size.
        - If expected_bytes or expected_chunk_index are provided (e.g. from persisted DB metadata),
          they must match the actual disk state.
        - Authoritative SHA-256 of the partial file is computed by streaming the .part
          file from disk in bounded reads (64 KiB). The in-memory accumulator is NOT used.

        Returns a validated TransferResumeRequestPayload.
        """
        if self._state != TransferState.INTERRUPTED:
            raise RuntimeError(
                f"Cannot prepare resume request in state {self._state.name}; transfer must be INTERRUPTED"
            )

        if self._temp_path is None or not self._temp_path.exists():
            raise FileNotFoundError(
                f"Partial staging file does not exist for transfer {self.meta.transfer_id}: {self._temp_path}"
            )

        disk_size = self._temp_path.stat().st_size
        if disk_size == 0:
            raise ValueError(
                f"Partial staging file is 0 bytes for transfer {self.meta.transfer_id}; cannot resume"
            )

        if self.meta.file_size <= 0:
            raise ValueError(
                f"Invalid file_size {self.meta.file_size} for transfer {self.meta.transfer_id}"
            )

        if disk_size > self.meta.file_size:
            raise ValueError(
                f"Partial file size ({disk_size}) exceeds declared file_size ({self.meta.file_size})"
            )

        if self.meta.chunk_size <= 0:
            raise ValueError(
                f"Invalid chunk_size {self.meta.chunk_size} for transfer {self.meta.transfer_id}"
            )

        if disk_size % self.meta.chunk_size != 0:
            raise ValueError(
                f"Partial file size {disk_size} is not aligned to chunk boundary "
                f"{self.meta.chunk_size} (remainder: {disk_size % self.meta.chunk_size})"
            )

        resume_chunk_index = disk_size // self.meta.chunk_size

        if expected_bytes is not None and expected_bytes != disk_size:
            raise ValueError(
                f"Inconsistent metadata: disk size {disk_size} != expected {expected_bytes}"
            )

        if expected_chunk_index is not None and expected_chunk_index != resume_chunk_index:
            raise ValueError(
                f"Inconsistent metadata: disk chunk index {resume_chunk_index} != expected {expected_chunk_index}"
            )
        # Compute SHA-256 by streaming the .part file from disk (bounded 64 KiB reads)
        hasher = hashlib.sha256()
        buf_size = 65536
        with open(self._temp_path, "rb") as f:
            while True:
                block = f.read(buf_size)
                if not block:
                    break
                hasher.update(block)

        partial_sha256 = hasher.hexdigest().lower()

        # Update local tracking to match disk state
        self._bytes_received = disk_size
        self._next_seq = resume_chunk_index

        return TransferResumeRequestPayload(
            transfer_id=self.meta.transfer_id,
            resume_offset_bytes=disk_size,
            resume_chunk_index=resume_chunk_index,
            partial_sha256=partial_sha256,
            protocol_version=1,
        )

    def resume(self, chunk_index: int) -> None:
        """
        Phase 3E Task 3 — Resume an interrupted incoming transfer.

        Semantics:
        - The transfer must be in INTERRUPTED or RESUME_REQUESTED state.
        - Re-opens the .part file in binary-append mode at its current end position.
        - Sets _next_seq to chunk_index so that the first resumed chunk frame is
          expected to carry seq == chunk_index (matching what the sender will send).
        - Resets the running SHA-256 hasher and re-feeds it from the .part file on
          disk so that finalise() can compute a valid full-file hash.
        - Transitions to RESUMING.

        After resume(), the caller should begin receiving chunks from the sender at
        the negotiated resume_chunk_index.  The binary FYCH seq numbers will start
        at chunk_index; receive_chunk() is tolerant of non-zero starting seq because
        we initialise _next_seq here.

        Raises:
          RuntimeError: if not in INTERRUPTED or RESUME_REQUESTED state.
          FileNotFoundError: if the .part file has disappeared from disk.
          TransferError: if the .part file cannot be re-opened for appending.
        """
        if self._state not in (TransferState.INTERRUPTED, TransferState.RESUME_REQUESTED):
            raise RuntimeError(
                f"Cannot resume from state {self._state.name}; "
                f"transfer must be INTERRUPTED or RESUME_REQUESTED"
            )

        # If called directly from INTERRUPTED (e.g. tests, or one-step path without
        # explicit request_resume()), pass through RESUME_REQUESTED first to satisfy
        # the state machine before transitioning to RESUMING.
        if self._state == TransferState.INTERRUPTED:
            self._state = _transfer_transition(self._state, TransferState.RESUME_REQUESTED)

        if self._temp_path is None or not self._temp_path.exists():
            self._state = TransferState.FAILED
            raise FileNotFoundError(
                f"Partial staging file lost for transfer {self.meta.transfer_id}: {self._temp_path}"
            )

        # Re-open the .part file in append mode (writes will go to the end).
        try:
            self._temp_fh = open(self._temp_path, "ab")
        except OSError as exc:
            self._state = TransferState.FAILED
            raise TransferError(
                f"Cannot re-open .part file for resume {self.meta.transfer_id[:8]}: {exc}"
            ) from exc

        # Re-initialise the SHA-256 accumulator from the bytes already on disk.
        # This is required so that finalise() computes a valid full-file hash.
        hasher = hashlib.sha256()
        try:
            with open(self._temp_path, "rb") as fh:
                for block in iter(lambda: fh.read(65536), b""):
                    hasher.update(block)
        except OSError as exc:
            # Non-fatal: we can still accumulate resumed chunks, but the
            # final hash will only cover resumed bytes — not the prefix.
            # This is acceptable; finalise() will reject a wrong hash.
            logger.warning(
                "Could not re-read .part prefix for SHA-256 seed for transfer %s: %s",
                self.meta.transfer_id[:8], exc,
            )
        self._hasher = hasher

        # Update tracking from disk-authoritative state.
        disk_size = self._temp_path.stat().st_size
        self._bytes_received = disk_size
        self._next_seq = chunk_index  # sender-confirmed chunk index

        self._state = _transfer_transition(self._state, TransferState.RESUMING)
        logger.info(
            "Incoming transfer %s RESUMING from chunk %d (offset %d bytes)",
            self.meta.transfer_id[:8], chunk_index, disk_size,
        )

    def cancel(self) -> None:
        """Cancel the transfer and remove temp file.

        Safe to call from any state; never raises.
        """
        if self._temp_fh:
            try:
                self._temp_fh.close()
            except Exception:
                pass
            self._temp_fh = None
        self._cleanup_temp()
        # Force into CANCELLED regardless of current state — cancel() must always succeed.
        self._state = TransferState.CANCELLED

    def _cleanup_temp(self) -> None:
        if self._temp_path and self._temp_path.exists():
            try:
                self._temp_path.unlink()
                logger.debug(
                    "Deleted staging file %s for transfer %s",
                    self._temp_path.name, self.meta.transfer_id[:8],
                )
            except Exception as exc:
                logger.warning(
                    "Could not delete staging file %s: %s",
                    self._temp_path, exc,
                )

    def _cleanup_on_io_error(self) -> None:
        """Close file handle, remove temp file, and set state to FAILED."""
        if self._temp_fh:
            try:
                self._temp_fh.close()
            except Exception:
                pass
            self._temp_fh = None
        self._cleanup_temp()
        self._state = TransferState.FAILED


# ─── Outgoing transfer session ────────────────────────────────────────────────

class OutgoingTransfer:
    """
    Manages one outgoing file transfer to a remote receiver.

    Reads the source file in CHUNK_SIZE chunks and yields encoded binary frames
    for the caller to encrypt and send.  Does not hold the file open between
    yields — streaming, not buffering.
    """

    def __init__(
        self,
        source_path: Path,
        receiver_identity: str,
        transfer_id: Optional[str] = None,
        batch_id: str = "",
        relative_path: str = "",
    ) -> None:
        self.transfer_id = transfer_id or str(uuid.uuid4())
        self._source_path = source_path
        self._receiver_identity = receiver_identity
        self.batch_id = batch_id
        self.relative_path = relative_path
        self._state = TransferState.IDLE
        self._bytes_sent: int = 0
        self._cancelled = False

    @property
    def state(self) -> TransferState:
        return self._state

    @property
    def bytes_sent(self) -> int:
        return self._bytes_sent

    def build_metadata(self, sender_identity: str) -> TransferMetadata:
        """
        Build transfer metadata.  Computes SHA-256 of source file.

        NOTE: This reads the entire file once to compute the hash.
        For large files this may take noticeable time but is unavoidable
        without a two-pass send (not implemented in Phase 3A MVP).
        """
        stat = self._source_path.stat()
        file_size = stat.st_size
        if file_size < 0:
            raise ValueError(f"Invalid negative file size: {self._source_path}")

        chunk_count = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE if file_size > 0 else 0
        sha256 = _sha256_file(self._source_path)
        mime_type = _guess_mime_type(self._source_path)

        return TransferMetadata(
            transfer_id=self.transfer_id,
            file_name=self._source_path.name,
            file_size=file_size,
            mime_type=mime_type,
            sha256=sha256,
            chunk_size=CHUNK_SIZE,
            chunk_count=chunk_count,
            sender_identity=sender_identity,
            created_at=int(time.time() * 1000),
            batch_id=self.batch_id,
            relative_path=self.relative_path,
        )

    async def stream_chunks(self) -> AsyncIterator[bytes]:
        """
        Async generator that yields encoded binary TRANSFER_CHUNK frames.

        The caller is responsible for encrypting each frame with the session's
        encrypt_frame() and writing to the transport.

        Phase 3D: Raises TransferError if the source file cannot be opened or read
        (e.g., file deleted mid-transfer, permission revoked, disk error).  State is
        set to FAILED before raising so the caller can detect the terminal condition.
        """
        async for frame_bytes in self._stream_chunks_internal(resume_chunk_index=0):
            yield frame_bytes

    async def stream_chunks_from(self, resume_chunk_index: int) -> AsyncIterator[bytes]:
        """
        Phase 3E Task 3 — Async generator that yields encoded TRANSFER_CHUNK frames
        starting from resume_chunk_index.

        The source file is seeked to resume_chunk_index * CHUNK_SIZE bytes before
        streaming begins.  FYCH seq numbers start at resume_chunk_index, not 0, so
        the receiver's _next_seq (already set to resume_chunk_index by resume()) is
        satisfied.

        Raises TransferError if the file cannot be seeked (e.g. offset beyond EOF)
        or if a read error occurs.  State is set to FAILED before raising.

        Precondition: the transfer must be in IDLE or TRANSFERRING state (managed by
        the caller: stream_chunks_from() sets TRANSFERRING immediately).
        """
        if resume_chunk_index < 0:
            raise ValueError(f"resume_chunk_index must be non-negative, got {resume_chunk_index}")
        async for frame_bytes in self._stream_chunks_internal(resume_chunk_index=resume_chunk_index):
            yield frame_bytes

    async def _stream_chunks_internal(
        self, resume_chunk_index: int
    ) -> AsyncIterator[bytes]:
        """
        Shared streaming implementation for stream_chunks() and stream_chunks_from().

        Opens the source file, seeks to resume_chunk_index * CHUNK_SIZE, and emits
        encoded FYCH frames with seq starting at resume_chunk_index.  On TransferError
        the caller should catch and handle; state is already FAILED.
        """
        self._state = _transfer_transition(self._state, TransferState.TRANSFERRING)
        seq = resume_chunk_index
        seek_offset = resume_chunk_index * CHUNK_SIZE
        try:
            fh = open(self._source_path, "rb")
        except OSError as exc:
            logger.error(
                "Cannot open source file %s for transfer %s: %s",
                self._source_path, self.transfer_id[:8], exc,
            )
            self._state = TransferState.FAILED
            raise TransferError(
                f"Cannot read source file for transfer {self.transfer_id[:8]}: {exc}"
            ) from exc
        try:
            if seek_offset > 0:
                try:
                    fh.seek(seek_offset)
                    actual_pos = fh.tell()
                    if actual_pos != seek_offset:
                        self._state = TransferState.FAILED
                        raise TransferError(
                            f"Seek to offset {seek_offset} failed for transfer "
                            f"{self.transfer_id[:8]}: landed at {actual_pos}"
                        )
                except OSError as exc:
                    self._state = TransferState.FAILED
                    raise TransferError(
                        f"Cannot seek source file for resume of transfer "
                        f"{self.transfer_id[:8]}: {exc}"
                    ) from exc
            while True:
                if self._cancelled:
                    self._state = TransferState.CANCELLING
                    return
                try:
                    chunk = fh.read(CHUNK_SIZE)
                except OSError as exc:
                    logger.error(
                        "Read error at seq=%d for transfer %s: %s",
                        seq, self.transfer_id[:8], exc,
                    )
                    self._state = TransferState.FAILED
                    raise TransferError(
                        f"Source read failed at seq {seq} for transfer {self.transfer_id[:8]}: {exc}"
                    ) from exc
                if not chunk:
                    break
                frame_bytes = encode_chunk_frame(self.transfer_id, seq, chunk)
                self._bytes_sent += len(chunk)
                seq += 1
                yield frame_bytes
        finally:
            fh.close()
        if not self._cancelled:
            self._state = _transfer_transition(self._state, TransferState.COMPLETED)

    def cancel(self) -> None:
        """Signal cancellation; the stream_chunks generator will stop on next iteration."""
        self._cancelled = True


# ─── Utility helpers ──────────────────────────────────────────────────────────

def _sha256_file(path: Path) -> str:
    """Compute hex SHA-256 digest of a file, reading in 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _guess_mime_type(path: Path) -> str:
    """Return a basic MIME type based on file extension."""
    import mimetypes
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"

# ─── Batch Transfer Classes (Phase 4C) ──────────────────────────────────────────

class BatchIncomingTransfer:
    def __init__(self, batch_id: str, batch_name: str, total_items: int, total_bytes: int):
        self.batch_id = batch_id
        self.batch_name = batch_name
        self.total_items = total_items
        self.total_bytes = total_bytes
        self.state = BatchState.REQUESTED
        self.items_completed = 0
        self.items_failed = 0
        self.bytes_transferred = 0

class BatchOutgoingTransfer:
    def __init__(self, batch_id: str, batch_name: str, total_items: int, total_bytes: int, paths: list[Path]):
        self.batch_id = batch_id
        self.batch_name = batch_name
        self.total_items = total_items
        self.total_bytes = total_bytes
        self.paths = paths
        self.state = BatchState.REQUESTED
        self.items_completed = 0
        self.items_failed = 0
        self.bytes_transferred = 0
