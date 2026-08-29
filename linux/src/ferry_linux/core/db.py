"""
SQLite Persistence Layer for Ferry.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, List, Optional


DB_SCHEMA_VERSION = 2  # Phase 2B: added identity_public_key_b64


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
    status: str     # "COMPLETED", "FAILED", "CANCELLED"
    started_at: int
    completed_at: Optional[int]
    sha256: str


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
