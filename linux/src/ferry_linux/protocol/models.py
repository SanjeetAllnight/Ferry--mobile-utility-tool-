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


@dataclass
class TransferErrorPayload:
    transfer_id: str
    error_code: str
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


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
