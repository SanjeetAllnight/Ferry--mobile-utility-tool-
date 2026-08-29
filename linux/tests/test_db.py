"""
Unit tests for Ferry SQLite Database Persistence.
"""

import tempfile
import time
import unittest
from pathlib import Path

from ferry_linux.core.db import DatabaseManager, TransferRecord, TrustedDevice


class TestDatabaseManager(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ferry.db"
        self.db = DatabaseManager(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_add_and_get_device(self) -> None:
        now = int(time.time())
        device = TrustedDevice(
            device_id="dev-12345",
            device_name="Test Phone",
            public_key="ed25519_base64_pubkey_abc",
            paired_at=now,
            last_seen=now,
        )
        self.db.add_or_update_device(device)

        retrieved = self.db.get_device("dev-12345")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.device_name, "Test Phone")
        self.assertEqual(retrieved.public_key, "ed25519_base64_pubkey_abc")

    def test_list_and_remove_devices(self) -> None:
        now = int(time.time())
        d1 = TrustedDevice("dev-1", "Phone 1", "key1", now, now)
        d2 = TrustedDevice("dev-2", "Phone 2", "key2", now, now + 10)
        self.db.add_or_update_device(d1)
        self.db.add_or_update_device(d2)

        devices = self.db.list_devices()
        self.assertEqual(len(devices), 2)
        # Should be ordered by last_seen DESC
        self.assertEqual(devices[0].device_id, "dev-2")

        self.db.remove_device("dev-1")
        devices_after = self.db.list_devices()
        self.assertEqual(len(devices_after), 1)
        self.assertEqual(devices_after[0].device_id, "dev-2")

    def test_add_and_list_transfers(self) -> None:
        now = int(time.time())
        device = TrustedDevice("dev-1", "Phone 1", "key1", now, now)
        self.db.add_or_update_device(device)

        record = TransferRecord(
            transfer_id="tx-999",
            device_id="dev-1",
            file_name="photo.jpg",
            file_size=1024000,
            direction="INCOMING",
            status="COMPLETED",
            started_at=now,
            completed_at=now + 5,
            sha256="abc123sha256",
        )
        self.db.add_transfer(record)

        transfers = self.db.list_transfers()
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].transfer_id, "tx-999")
        self.assertEqual(transfers[0].file_name, "photo.jpg")
        self.assertEqual(transfers[0].status, "COMPLETED")


if __name__ == "__main__":
    unittest.main()
