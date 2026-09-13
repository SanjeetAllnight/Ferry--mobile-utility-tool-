"""
SQLite Persistence Layer for Ferry.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, List, Optional


DB_SCHEMA_VERSION = 3  # Phase 3E Task 1: added resumable transfer columns


@dataclass
class TrustedDevice:
    device_id: str
    device_name: str
    public_key: str          # legacy alias kept for compatibility
    identity_public_key_b64: str  # base64url Ed25519 public key (Phase 2B)
    paired_at: int
    last_seen: int


@dataclass
class TransferRecord:
    transfer_id: str
    device_id: str
    file_name: str
    file_size: int
    direction: str  # "INCOMING" or "OUTGOING"
    status: str     # "COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"
    started_at: int
    completed_at: Optional[int]
    sha256: str


# ── Phase 3E Task 1: Interrupted transfer persistence ─────────────────────────

@dataclass
class InterruptedTransferInfo:
    """
    Resumable state for an interrupted incoming transfer.

    This dataclass captures all information required to resume a transfer:
    - bytes_received:         how many bytes are safely written to the .part file
    - resume_chunk_index:     the next chunk seq the receiver expects
    - partial_sha256:         SHA-256 of the .part file contents (computed at interrupt time)
    - sender_identity:        Ed25519 public key of the original sender (base64url)
    - original_metadata_json: JSON-encoded TransferMetadata blob
    - interrupted_at:         epoch-ms when the interrupt occurred
    - expire_at:              epoch-ms after which the .part file may be cleaned up
                              (default: interrupted_at + 7 days)
    """
    transfer_id: str
    bytes_received: int
    resume_chunk_index: int
    partial_sha256: str          # hex SHA-256 of .part bytes written so far
    sender_identity: str         # base64url Ed25519 public key
    original_metadata_json: str  # JSON blob of TransferMetadata.to_dict()
    interrupted_at: int          # epoch-ms
    expire_at: int               # epoch-ms

    # 7-day TTL constant (milliseconds)
    RESUME_TTL_MS: int = 7 * 24 * 60 * 60 * 1000

    @staticmethod
    def make_expire_at(interrupted_at_ms: int) -> int:
        """Return the default expiry (7 days after interruption)."""
        return interrupted_at_ms + InterruptedTransferInfo.RESUME_TTL_MS


class DatabaseManager:
    """Manages the local SQLite database for Ferry."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create database schema and apply migrations if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            cursor = conn.cursor()

            # Schema version tracking table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY
                )
            """)

            cursor.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
            row = cursor.fetchone()
            current_version = row[0] if row else 0

            if current_version < 1:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS trusted_devices (
                        device_id TEXT PRIMARY KEY,
                        device_name TEXT NOT NULL,
                        public_key TEXT NOT NULL,
                        identity_public_key_b64 TEXT NOT NULL DEFAULT '',
                        paired_at INTEGER NOT NULL,
                        last_seen INTEGER NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS transfer_history (
                        transfer_id TEXT PRIMARY KEY,
                        device_id TEXT NOT NULL,
                        file_name TEXT NOT NULL,
                        file_size INTEGER NOT NULL,
                        direction TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at INTEGER NOT NULL,
                        completed_at INTEGER,
                        sha256 TEXT NOT NULL,
                        FOREIGN KEY(device_id) REFERENCES trusted_devices(device_id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)

            if current_version < 2:
                # Phase 2B: add identity_public_key_b64 if column missing (migration from v1)
                try:
                    cursor.execute("""
                        ALTER TABLE trusted_devices
                        ADD COLUMN identity_public_key_b64 TEXT NOT NULL DEFAULT ''
                    """)
                except Exception:
                    pass  # Column already exists

            if current_version < 3:
                # Phase 3E Task 1: add resumable transfer columns to transfer_history
                # All new columns default to NULL so existing rows remain valid.
                _resume_columns = [
                    ("interrupted_at",         "INTEGER", "NULL"),
                    ("bytes_received",          "INTEGER", "NULL"),
                    ("resume_chunk_index",      "INTEGER", "NULL"),
                    ("partial_sha256",          "TEXT",    "NULL"),
                    ("sender_identity",         "TEXT",    "NULL"),
                    ("original_metadata_json",  "TEXT",    "NULL"),
                    ("expire_at",               "INTEGER", "NULL"),
                ]
                for col_name, col_type, default in _resume_columns:
                    try:
                        cursor.execute(
                            f"ALTER TABLE transfer_history "
                            f"ADD COLUMN {col_name} {col_type} DEFAULT {default}"
                        )
                    except Exception:
                        pass  # Column already exists — idempotent

            cursor.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (DB_SCHEMA_VERSION,)
            )
            conn.commit()

    def add_or_update_device(self, device: TrustedDevice) -> None:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trusted_devices
                    (device_id, device_name, public_key, identity_public_key_b64, paired_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_name=excluded.device_name,
                    public_key=excluded.public_key,
                    identity_public_key_b64=excluded.identity_public_key_b64,
                    last_seen=excluded.last_seen
            """, (
                device.device_id,
                device.device_name,
                device.public_key,
                device.identity_public_key_b64,
                device.paired_at,
                device.last_seen,
            ))
            conn.commit()

    def get_device(self, device_id: str) -> Optional[TrustedDevice]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trusted_devices WHERE device_id = ?", (device_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return TrustedDevice(
                device_id=row["device_id"],
                device_name=row["device_name"],
                public_key=row["public_key"],
                identity_public_key_b64=row["identity_public_key_b64"],
                paired_at=row["paired_at"],
                last_seen=row["last_seen"],
            )

    def get_device_by_public_key(self, identity_public_key_b64: str) -> Optional[TrustedDevice]:
        """Look up a trusted device by its Ed25519 public key (base64url)."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trusted_devices WHERE identity_public_key_b64 = ?",
                (identity_public_key_b64,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return TrustedDevice(
                device_id=row["device_id"],
                device_name=row["device_name"],
                public_key=row["public_key"],
                identity_public_key_b64=row["identity_public_key_b64"],
                paired_at=row["paired_at"],
                last_seen=row["last_seen"],
            )

    def list_devices(self) -> List[TrustedDevice]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trusted_devices ORDER BY last_seen DESC")
            return [
                TrustedDevice(
                    device_id=row["device_id"],
                    device_name=row["device_name"],
                    public_key=row["public_key"],
                    identity_public_key_b64=row["identity_public_key_b64"],
                    paired_at=row["paired_at"],
                    last_seen=row["last_seen"],
                )
                for row in cursor.fetchall()
            ]

    def remove_device(self, device_id: str) -> None:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trusted_devices WHERE device_id = ?", (device_id,))
            conn.commit()

    def remove_device_by_public_key(self, identity_public_key_b64: str) -> None:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trusted_devices WHERE identity_public_key_b64 = ?", (identity_public_key_b64,))
            conn.commit()

    def add_transfer(self, record: TransferRecord) -> None:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO transfer_history (
                    transfer_id, device_id, file_name, file_size, direction, status, started_at, completed_at, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transfer_id) DO UPDATE SET
                    status=excluded.status,
                    completed_at=excluded.completed_at
            """, (
                record.transfer_id, record.device_id, record.file_name, record.file_size,
                record.direction, record.status, record.started_at, record.completed_at, record.sha256
            ))
            conn.commit()

    def list_transfers(self, limit: int = 50) -> List[TransferRecord]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transfer_history ORDER BY started_at DESC LIMIT ?", (limit,))
            return [
                TransferRecord(
                    transfer_id=row["transfer_id"],
                    device_id=row["device_id"],
                    file_name=row["file_name"],
                    file_size=row["file_size"],
                    direction=row["direction"],
                    status=row["status"],
                    started_at=row["started_at"],
                    completed_at=row["completed_at"],
                    sha256=row["sha256"],
                )
                for row in cursor.fetchall()
            ]

    # ── Phase 3E Task 1: Interrupted transfer management ──────────────────────

    def save_interrupted_transfer(self, info: InterruptedTransferInfo) -> None:
        """
        Persist resumable state for an interrupted incoming transfer.

        Updates an existing transfer_history row (which must already exist with
        status INTERRUPTED) with the resume columns, or inserts a minimal row if
        none exists.  Either way the row is left with status='INTERRUPTED'.

        This is safe to call multiple times for the same transfer_id — each call
        overwrites the resume columns with the latest values.
        """
        now = int(time.time() * 1000)
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT transfer_id FROM transfer_history WHERE transfer_id = ?",
                (info.transfer_id,),
            )
            existing = cursor.fetchone()

            if existing:
                cursor.execute("""
                    UPDATE transfer_history SET
                        status                = 'INTERRUPTED',
                        interrupted_at        = ?,
                        bytes_received        = ?,
                        resume_chunk_index    = ?,
                        partial_sha256        = ?,
                        sender_identity       = ?,
                        original_metadata_json = ?,
                        expire_at             = ?
                    WHERE transfer_id = ?
                """, (
                    info.interrupted_at,
                    info.bytes_received,
                    info.resume_chunk_index,
                    info.partial_sha256,
                    info.sender_identity,
                    info.original_metadata_json,
                    info.expire_at,
                    info.transfer_id,
                ))
            else:
                # Row doesn't exist yet — insert a minimal record.
                # This can happen if the service was interrupted before the
                # initial transfer_history row was written.
                cursor.execute("""
                    INSERT INTO transfer_history (
                        transfer_id, device_id, file_name, file_size,
                        direction, status, started_at, completed_at, sha256,
                        interrupted_at, bytes_received, resume_chunk_index,
                        partial_sha256, sender_identity, original_metadata_json,
                        expire_at
                    ) VALUES (?, '', '', 0, 'INCOMING', 'INTERRUPTED', ?, NULL, '',
                              ?, ?, ?, ?, ?, ?, ?)
                """, (
                    info.transfer_id,
                    now,
                    info.interrupted_at,
                    info.bytes_received,
                    info.resume_chunk_index,
                    info.partial_sha256,
                    info.sender_identity,
                    info.original_metadata_json,
                    info.expire_at,
                ))
            conn.commit()

    def get_interrupted_transfer(self, transfer_id: str) -> Optional[InterruptedTransferInfo]:
        """
        Retrieve persisted resume state for a single interrupted transfer.
        Returns None if no INTERRUPTED row exists for this transfer_id.
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT transfer_id, bytes_received, resume_chunk_index,
                       partial_sha256, sender_identity, original_metadata_json,
                       interrupted_at, expire_at
                FROM transfer_history
                WHERE transfer_id = ? AND status = 'INTERRUPTED'
            """, (transfer_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return InterruptedTransferInfo(
                transfer_id=row["transfer_id"],
                bytes_received=row["bytes_received"] or 0,
                resume_chunk_index=row["resume_chunk_index"] or 0,
                partial_sha256=row["partial_sha256"] or "",
                sender_identity=row["sender_identity"] or "",
                original_metadata_json=row["original_metadata_json"] or "",
                interrupted_at=row["interrupted_at"] or 0,
                expire_at=row["expire_at"] or 0,
            )

    def list_interrupted_transfers_for_peer(self, sender_identity: str) -> List[InterruptedTransferInfo]:
        """
        Return all INTERRUPTED incoming transfer records matching a sender identity.
        Used on session re-establishment to discover resumable transfers.
        """
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT transfer_id, bytes_received, resume_chunk_index,
                       partial_sha256, sender_identity, original_metadata_json,
                       interrupted_at, expire_at
                FROM transfer_history
                WHERE status = 'INTERRUPTED' AND sender_identity = ?
                ORDER BY interrupted_at DESC
            """, (sender_identity,))
            return [
                InterruptedTransferInfo(
                    transfer_id=row["transfer_id"],
                    bytes_received=row["bytes_received"] or 0,
                    resume_chunk_index=row["resume_chunk_index"] or 0,
                    partial_sha256=row["partial_sha256"] or "",
                    sender_identity=row["sender_identity"] or "",
                    original_metadata_json=row["original_metadata_json"] or "",
                    interrupted_at=row["interrupted_at"] or 0,
                    expire_at=row["expire_at"] or 0,
                )
                for row in cursor.fetchall()
            ]

    def delete_interrupted_transfer(self, transfer_id: str) -> None:
        """Remove a specific interrupted transfer record from the DB (set to FAILED)."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE transfer_history
                SET status = 'FAILED',
                    interrupted_at = NULL,
                    bytes_received = NULL,
                    resume_chunk_index = NULL,
                    partial_sha256 = NULL,
                    sender_identity = NULL,
                    original_metadata_json = NULL,
                    expire_at = NULL
                WHERE transfer_id = ? AND status = 'INTERRUPTED'
            """, (transfer_id,))
            conn.commit()

    def expire_interrupted_transfers(self, now_ms: Optional[int] = None) -> int:
        """
        Delete .part metadata (set status→FAILED) for all INTERRUPTED transfers
        whose expire_at has passed.  Returns the number of rows updated.

        Callers are responsible for also deleting the actual .part file from disk
        before or after calling this method.
        """
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE transfer_history
                SET status = 'FAILED',
                    interrupted_at = NULL,
                    bytes_received = NULL,
                    resume_chunk_index = NULL,
                    partial_sha256 = NULL,
                    sender_identity = NULL,
                    original_metadata_json = NULL,
                    expire_at = NULL
                WHERE status = 'INTERRUPTED' AND expire_at IS NOT NULL AND expire_at <= ?
            """, (now_ms,))
            count = cursor.rowcount
            conn.commit()
            return count
