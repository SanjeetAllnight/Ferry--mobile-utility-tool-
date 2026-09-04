"""
Ferry Transfer Layer — Phase 3A.

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


# ─── Transfer State Machine ────────────────────────────────────────────────────

class TransferState(Enum):
    """Explicit state machine for one file transfer."""
    IDLE = auto()
    REQUESTED = auto()      # TRANSFER_REQUEST sent/received; awaiting decision
    ACCEPTED = auto()       # TRANSFER_ACCEPT exchanged; ready to stream
    TRANSFERRING = auto()   # Chunks flowing
    CANCELLING = auto()     # TRANSFER_CANCEL sent; draining
    COMPLETED = auto()      # SHA-256 verified and file finalised
    FAILED = auto()         # Error (IO, integrity, protocol)
    CANCELLED = auto()      # Cancelled by either side

    VALID_TRANSITIONS: dict  # defined below

# Define valid state transitions after the class body
TransferState.VALID_TRANSITIONS = {
    TransferState.IDLE:        {TransferState.REQUESTED, TransferState.ACCEPTED,
                                TransferState.TRANSFERRING},
    TransferState.REQUESTED:   {TransferState.ACCEPTED, TransferState.FAILED,
                                TransferState.CANCELLED},
    TransferState.ACCEPTED:    {TransferState.TRANSFERRING, TransferState.FAILED,
                                TransferState.CANCELLED},
    TransferState.TRANSFERRING:{TransferState.CANCELLING, TransferState.COMPLETED,
                                TransferState.FAILED},
    TransferState.CANCELLING:  {TransferState.CANCELLED, TransferState.FAILED},
    TransferState.COMPLETED:   set(),
    TransferState.FAILED:      set(),
    TransferState.CANCELLED:   set(),
}


def _transfer_transition(current: TransferState, new: TransferState) -> TransferState:
    allowed = TransferState.VALID_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise RuntimeError(
            f"Invalid transfer state transition: {current.name} → {new.name}"
        )
    return new


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


def resolve_safe_destination(staging_dir: Path, file_name: str) -> Path:
    """
    Resolve the safe staging destination for a received file.

    Raises SecurityError if the resolved path escapes staging_dir.
    """
    sanitised = sanitise_filename(file_name)
    if not sanitised:
        raise ValueError(f"Cannot resolve safe destination for {file_name!r}")
    dest = (staging_dir / sanitised).resolve()
    staging_resolved = staging_dir.resolve()
    if not str(dest).startswith(str(staging_resolved) + os.sep) and dest != staging_resolved:
        raise SecurityError(f"Path traversal detected for {file_name!r} → {dest}")
    return dest


class SecurityError(Exception):
    """Raised when a security constraint is violated."""


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
        """Open the temp file and transition to ACCEPTED."""
        self._state = _transfer_transition(self._state, TransferState.ACCEPTED)
        staging_dir = self._staging_dir
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._download_dir.mkdir(parents=True, exist_ok=True)
        temp_name = f"{self.meta.transfer_id}.part"
        self._temp_path = staging_dir / temp_name
        self._final_path = resolve_safe_destination(self._download_dir, self.meta.file_name)
        self._temp_fh = open(self._temp_path, "wb")
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
        Raises RuntimeError if not in TRANSFERRING state.
        """
        if self._state != TransferState.TRANSFERRING:
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

        self._temp_fh.write(frame.data)
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

    def cancel(self) -> None:
        """Cancel the transfer and remove temp file."""
        if self._temp_fh:
            try:
                self._temp_fh.close()
            except Exception:
                pass
            self._temp_fh = None
        self._cleanup_temp()
        try:
            self._state = _transfer_transition(self._state, TransferState.CANCELLING)
            self._state = _transfer_transition(self._state, TransferState.CANCELLED)
        except RuntimeError:
            self._state = TransferState.CANCELLED

    def _cleanup_temp(self) -> None:
        if self._temp_path and self._temp_path.exists():
            try:
                self._temp_path.unlink()
            except Exception:
                pass


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
    ) -> None:
        self.transfer_id = transfer_id or str(uuid.uuid4())
        self._source_path = source_path
        self._receiver_identity = receiver_identity
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
        )

    async def stream_chunks(self) -> AsyncIterator[bytes]:
        """
        Async generator that yields encoded binary TRANSFER_CHUNK frames.

        The caller is responsible for encrypting each frame with the session's
        encrypt_frame() and writing to the transport.
        """
        self._state = _transfer_transition(self._state, TransferState.TRANSFERRING)
        seq = 0
        with open(self._source_path, "rb") as fh:
            while True:
                if self._cancelled:
                    self._state = TransferState.CANCELLING
                    return
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                frame_bytes = encode_chunk_frame(self.transfer_id, seq, chunk)
                self._bytes_sent += len(chunk)
                seq += 1
                yield frame_bytes
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
