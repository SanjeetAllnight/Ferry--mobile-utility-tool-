"""
Ferry Wire Protocol Data Structures, Envelopes, and Binary Framing.

Control frames: [2B magic 'FY'] + [4B uint32 length] + [JSON envelope]
Data frames (TRANSFER_CHUNK): delivered inside the AEAD layer as a binary
  plaintext starting with 'FYCH' — see ferry_linux.core.transfer for details.
"""

from __future__ import annotations

import json
import struct
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

MAGIC_BYTES = b"FY"  # 0x46 0x59
PROTOCOL_VERSION = 1
MAX_CONTROL_FRAME_SIZE = 1048576  # 1 MiB


class MessageType(str, Enum):
    # Connection / Session (Phase 2B)
    HELLO = "HELLO"
    AUTH_CHALLENGE = "AUTH_CHALLENGE"
    AUTH_RESPONSE = "AUTH_RESPONSE"
    SESSION_ESTABLISHED = "SESSION_ESTABLISHED"
    SESSION_ERROR = "SESSION_ERROR"
    DISCONNECT = "DISCONNECT"

    # Handshake & Pairing
    HANDSHAKE_INIT = "HANDSHAKE_INIT"
    HANDSHAKE_RESPONSE = "HANDSHAKE_RESPONSE"
    PAIR_REQUEST = "PAIR_REQUEST"
    PAIR_CONFIRM = "PAIR_CONFIRM"
    PAIR_DECISION = "PAIR_DECISION"

    # File Transfer (Phase 3A)
    TRANSFER_REQUEST = "TRANSFER_REQUEST"    # sender → receiver: metadata
    TRANSFER_ACCEPT = "TRANSFER_ACCEPT"      # receiver → sender: go ahead
    TRANSFER_REJECT = "TRANSFER_REJECT"      # receiver → sender: declined
    TRANSFER_CHUNK = "TRANSFER_CHUNK"        # sender → receiver: binary data frame (in-band)
    TRANSFER_PROGRESS = "TRANSFER_PROGRESS"  # sender → receiver: progress update
    TRANSFER_CANCEL = "TRANSFER_CANCEL"      # either direction: abort
    TRANSFER_COMPLETE = "TRANSFER_COMPLETE"  # sender → receiver: all chunks sent
    TRANSFER_RESULT = "TRANSFER_RESULT"      # receiver → sender: integrity verdict
    TRANSFER_ERROR = "TRANSFER_ERROR"        # either direction: fatal error

    # Resumable File Transfer (Phase 3E Task 2)
    TRANSFER_RESUME_REQUEST = "TRANSFER_RESUME_REQUEST"  # receiver → sender: negotiate resume
    TRANSFER_RESUME_ACCEPT = "TRANSFER_RESUME_ACCEPT"    # sender → receiver: accept resume
    TRANSFER_RESUME_REJECT = "TRANSFER_RESUME_REJECT"    # sender → receiver: reject resume

    # Multi-File / Directory Batch Transfers (Phase 4C)
    BATCH_REQUEST = "BATCH_REQUEST"          # sender → receiver: batch metadata
    BATCH_ACCEPT = "BATCH_ACCEPT"            # receiver → sender: accept batch
    BATCH_REJECT = "BATCH_REJECT"            # receiver → sender: reject batch
    BATCH_CANCEL = "BATCH_CANCEL"            # either direction: abort batch
    BATCH_COMPLETE = "BATCH_COMPLETE"        # sender → receiver: all items sent

    # Clipboard Sync (MVP Final Sprint)
    CLIPBOARD_SYNC = "CLIPBOARD_SYNC"        # either direction: push text clipboard to peer
    CLIPBOARD_SYNC_ACK = "CLIPBOARD_SYNC_ACK"  # optional acknowledgement


@dataclass
class TransferRequestPayload:
    """
    Phase 3A TRANSFER_REQUEST payload — single file per transfer (MVP).
    Validated by TransferMetadata.from_dict() before any filesystem use.
    """
    transfer_id: str
    file_name: str        # basename only
    file_size: int
    mime_type: str
    sha256: str           # hex SHA-256 of complete file
    chunk_size: int
    chunk_count: int
    sender_identity: str  # base64url Ed25519 public key
    created_at: int       # ms epoch
    protocol_version: int = PROTOCOL_VERSION
    batch_id: str = ""    # If non-empty, belongs to a batch
    relative_path: str = "" # If non-empty, relative path within batch/directory

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransferRequestPayload":
        return cls(
            transfer_id=str(data["transfer_id"]),
            file_name=str(data["file_name"]),
            file_size=int(data["file_size"]),
            mime_type=str(data.get("mime_type", "application/octet-stream")),
            sha256=str(data.get("sha256", "")),
            chunk_size=int(data.get("chunk_size", 65536)),
            chunk_count=int(data.get("chunk_count", 0)),
            sender_identity=str(data.get("sender_identity", "")),
            created_at=int(data.get("created_at", 0)),
            protocol_version=int(data.get("protocol_version", PROTOCOL_VERSION)),
            batch_id=str(data.get("batch_id", "")),
            relative_path=str(data.get("relative_path", "")),
        )


@dataclass
class TransferAcceptPayload:
    transfer_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TransferRejectPayload:
    transfer_id: str
    reason: str   # e.g. "USER_REJECTED", "BUSY", "INSUFFICIENT_STORAGE"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TransferProgressPayload:
    transfer_id: str
    bytes_transferred: int
    total_bytes: int
    speed_bytes_per_sec: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TransferCancelPayload:
    transfer_id: str
    reason: str = "USER_CANCELLED"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TransferCompletePayload:
    """Sender → receiver: all chunks have been sent."""
    transfer_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TransferResultPayload:
    """Receiver → sender: integrity verdict after receiving all chunks."""
    transfer_id: str
    success: bool
    sha256: str      # hex SHA-256 computed by receiver
    error: str = "" # human-readable error if success=False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Phase 4C Batch Protocol Models ────────────────────────────────────────

@dataclass
class BatchRequestPayload:
    batch_id: str
    batch_name: str
    total_items: int
    total_bytes: int      # -1 if indeterminate
    sender_identity: str
    created_at: int
    protocol_version: int = PROTOCOL_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BatchRequestPayload":
        return cls(
            batch_id=str(data["batch_id"]),
            batch_name=str(data["batch_name"]),
            total_items=int(data["total_items"]),
            total_bytes=int(data["total_bytes"]),
            sender_identity=str(data.get("sender_identity", "")),
            created_at=int(data.get("created_at", 0)),
            protocol_version=int(data.get("protocol_version", PROTOCOL_VERSION)),
        )

@dataclass
class BatchAcceptPayload:
    batch_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class BatchRejectPayload:
    batch_id: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class BatchCancelPayload:
    batch_id: str
    reason: str = "USER_CANCELLED"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class BatchCompletePayload:
    batch_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TransferErrorPayload:
    transfer_id: str
    error_code: str
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ResumeRejectReason(str, Enum):
    """
    Phase 3E Task 2 allowed reasons for TRANSFER_RESUME_REJECT.
    """
    SOURCE_MODIFIED = "SOURCE_MODIFIED"
    TRANSFER_NOT_FOUND = "TRANSFER_NOT_FOUND"
    PARTIAL_CORRUPT = "PARTIAL_CORRUPT"
    WRONG_PEER = "WRONG_PEER"
    STALE = "STALE"
    PEER_CANCELLED = "PEER_CANCELLED"


@dataclass
class TransferResumeRequestPayload:
    """
    Phase 3E Task 2 TRANSFER_RESUME_REQUEST payload.
    Sent by the receiver to request resumption of an interrupted transfer.
    """
    transfer_id: str
    resume_offset_bytes: int
    resume_chunk_index: int
    partial_sha256: str
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self, chunk_size: int = 65536) -> None:
        # Validate UUID format
        if not isinstance(self.transfer_id, str) or not self.transfer_id:
            raise ValueError(f"transfer_id must be a non-empty UUID string, got {self.transfer_id!r}")
        try:
            uuid.UUID(self.transfer_id)
        except Exception as exc:
            raise ValueError(f"Invalid transfer_id UUID format: {self.transfer_id!r}") from exc

        # Non-negative offset
        if not isinstance(self.resume_offset_bytes, int) or isinstance(self.resume_offset_bytes, bool) or self.resume_offset_bytes < 0:
            raise ValueError(f"resume_offset_bytes must be a non-negative integer, got {self.resume_offset_bytes!r}")
        if self.resume_offset_bytes > 10 * 1024 ** 3:
            raise ValueError(f"resume_offset_bytes exceeds reasonable bound (10 GiB limit): {self.resume_offset_bytes}")

        # Non-negative chunk index
        if not isinstance(self.resume_chunk_index, int) or isinstance(self.resume_chunk_index, bool) or self.resume_chunk_index < 0:
            raise ValueError(f"resume_chunk_index must be a non-negative integer, got {self.resume_chunk_index!r}")

        # Protocol version
        if not isinstance(self.protocol_version, int) or isinstance(self.protocol_version, bool) or self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"Invalid protocol_version: {self.protocol_version!r} (expected {PROTOCOL_VERSION})")

        # 64-character lowercase hex SHA-256
        if not isinstance(self.partial_sha256, str):
            raise ValueError("partial_sha256 must be a string")
        if len(self.partial_sha256) != 64 or not all(c in "0123456789abcdef" for c in self.partial_sha256):
            raise ValueError(f"partial_sha256 must be a 64-character lowercase hex string: {self.partial_sha256!r}")

        # Consistency between offset and chunk index
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")
        if self.resume_chunk_index == 0 and self.resume_offset_bytes != 0:
            raise ValueError(
                f"Offset/chunk mismatch: resume_chunk_index is 0 but resume_offset_bytes is {self.resume_offset_bytes}"
            )
        if self.resume_offset_bytes != self.resume_chunk_index * chunk_size:
            raise ValueError(
                f"Offset/chunk mismatch: resume_offset_bytes {self.resume_offset_bytes} != "
                f"resume_chunk_index {self.resume_chunk_index} * chunk_size {chunk_size} "
                f"(expected {self.resume_chunk_index * chunk_size})"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    @classmethod
    def from_dict(cls, data: Dict[str, Any], chunk_size: int = 65536) -> "TransferResumeRequestPayload":
        required = ("transfer_id", "resume_offset_bytes", "resume_chunk_index", "partial_sha256", "protocol_version")
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Missing required field in TRANSFER_RESUME_REQUEST payload: {field_name}")

        inst = cls(
            transfer_id=str(data["transfer_id"]),
            resume_offset_bytes=data["resume_offset_bytes"] if isinstance(data["resume_offset_bytes"], int) and not isinstance(data["resume_offset_bytes"], bool) else int(data["resume_offset_bytes"]),
            resume_chunk_index=data["resume_chunk_index"] if isinstance(data["resume_chunk_index"], int) and not isinstance(data["resume_chunk_index"], bool) else int(data["resume_chunk_index"]),
            partial_sha256=str(data["partial_sha256"]),
            protocol_version=data["protocol_version"] if isinstance(data["protocol_version"], int) and not isinstance(data["protocol_version"], bool) else int(data["protocol_version"]),
        )
        inst.validate(chunk_size=chunk_size)
        return inst


@dataclass
class TransferResumeAcceptPayload:
    """
    Phase 3E Task 2 TRANSFER_RESUME_ACCEPT payload.
    Sent by the sender confirming resume acceptance.
    """
    transfer_id: str
    resume_chunk_index: int
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.transfer_id, str) or not self.transfer_id:
            raise ValueError(f"transfer_id must be a non-empty UUID string, got {self.transfer_id!r}")
        try:
            uuid.UUID(self.transfer_id)
        except Exception as exc:
            raise ValueError(f"Invalid transfer_id UUID format: {self.transfer_id!r}") from exc

        if not isinstance(self.resume_chunk_index, int) or isinstance(self.resume_chunk_index, bool) or self.resume_chunk_index < 0:
            raise ValueError(f"resume_chunk_index must be a non-negative integer, got {self.resume_chunk_index!r}")

        if not isinstance(self.protocol_version, int) or isinstance(self.protocol_version, bool) or self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"Invalid protocol_version: {self.protocol_version!r} (expected {PROTOCOL_VERSION})")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransferResumeAcceptPayload":
        required = ("transfer_id", "resume_chunk_index", "protocol_version")
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Missing required field in TRANSFER_RESUME_ACCEPT payload: {field_name}")

        inst = cls(
            transfer_id=str(data["transfer_id"]),
            resume_chunk_index=data["resume_chunk_index"] if isinstance(data["resume_chunk_index"], int) and not isinstance(data["resume_chunk_index"], bool) else int(data["resume_chunk_index"]),
            protocol_version=data["protocol_version"] if isinstance(data["protocol_version"], int) and not isinstance(data["protocol_version"], bool) else int(data["protocol_version"]),
        )
        inst.validate()
        return inst


@dataclass
class TransferResumeRejectPayload:
    """
    Phase 3E Task 2 TRANSFER_RESUME_REJECT payload.
    Sent by the sender declining resume.
    """
    transfer_id: str
    reason: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.transfer_id, str) or not self.transfer_id:
            raise ValueError(f"transfer_id must be a non-empty UUID string, got {self.transfer_id!r}")
        try:
            uuid.UUID(self.transfer_id)
        except Exception as exc:
            raise ValueError(f"Invalid transfer_id UUID format: {self.transfer_id!r}") from exc

        allowed = {r.value for r in ResumeRejectReason}
        if self.reason not in allowed:
            raise ValueError(f"Invalid reject reason: {self.reason!r} (allowed: {sorted(allowed)})")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransferResumeRejectPayload":
        required = ("transfer_id", "reason")
        for field_name in required:
            if field_name not in data:
                raise ValueError(f"Missing required field in TRANSFER_RESUME_REJECT payload: {field_name}")

        inst = cls(
            transfer_id=str(data["transfer_id"]),
            reason=str(data["reason"]),
        )
        inst.validate()
        return inst


@dataclass
class FerryEnvelope:
    type: str
    payload: Dict[str, Any]
    protocol_version: int = PROTOCOL_VERSION
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reply_to: Optional[str] = None
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_id": self.message_id,
            "reply_to": self.reply_to,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FerryEnvelope:
        return cls(
            protocol_version=data.get("protocol_version", PROTOCOL_VERSION),
            message_id=data.get("message_id", str(uuid.uuid4())),
            reply_to=data.get("reply_to"),
            timestamp=data.get("timestamp", int(time.time() * 1000)),
            type=data["type"],
            payload=data.get("payload", {}),
        )

    @classmethod
    def from_json(cls, json_str: str) -> FerryEnvelope:
        data = json.loads(json_str)
        return cls.from_dict(data)


def encode_frame(envelope: FerryEnvelope) -> bytes:
    """
    Encodes a FerryEnvelope into binary frame bytes:
    [2 bytes MAGIC 'FY'] + [4 bytes uint32 big-endian length] + [JSON payload]
    """
    payload_bytes = envelope.to_json().encode("utf-8")
    length = len(payload_bytes)
    if length > MAX_CONTROL_FRAME_SIZE:
        raise ValueError(f"Payload length {length} exceeds max frame size {MAX_CONTROL_FRAME_SIZE}")
    header = MAGIC_BYTES + struct.pack("!I", length)
    return header + payload_bytes


def decode_frame(buffer: bytes) -> Tuple[Optional[FerryEnvelope], int]:
    """
    Attempts to decode a frame from a byte buffer.
    Returns (envelope, bytes_consumed).
    If buffer is incomplete, returns (None, 0).
    If magic header is invalid, raises ValueError.
    """
    if len(buffer) < 6:
        return None, 0

    magic = buffer[:2]
    if magic != MAGIC_BYTES:
        raise ValueError(f"Invalid magic bytes: {magic!r}")

    (length,) = struct.unpack("!I", buffer[2:6])
    if length > MAX_CONTROL_FRAME_SIZE:
        raise ValueError(f"Frame length {length} exceeds maximum allowed size")

    total_frame_size = 6 + length
    if len(buffer) < total_frame_size:
        return None, 0  # Incomplete frame

    payload_bytes = buffer[6:total_frame_size]
    json_str = payload_bytes.decode("utf-8")
    envelope = FerryEnvelope.from_json(json_str)
    return envelope, total_frame_size
