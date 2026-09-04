import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ferry_linux.core.config import FerryConfig
from ferry_linux.core.service import FerryService, PeerSession
from ferry_linux.core.session import FerrySession
from ferry_linux.core.transfer import IncomingTransfer, TransferMetadata, TransferState, encode_chunk_frame
from ferry_linux.protocol.models import MessageType, FerryEnvelope


class TestReceiverLogic(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = FerryConfig(download_dir=self.temp_dir)
        self.service = FerryService()
        self.service.config = self.config
        
        self.reader = AsyncMock()
        self.writer = AsyncMock()
        
        # Setup a mock session that is ESTABLISHED
        self.ferry_session = MagicMock(spec=FerrySession)
        self.ferry_session.state = 5  # SessionState.ESTABLISHED
        self.ps = PeerSession(self.ferry_session, self.reader, self.writer, "192.168.1.100:5000")
        self.ps.remote_device_name = "TestPhone"
        self.service._active_sessions["192.168.1.100:5000"] = self.ps
        
        # Patch send_encrypted so we can assert on outgoing messages
        self.service.send_encrypted = AsyncMock()
        
        # A valid TransferMetadata dictionary
        self.valid_meta_dict = {
            "transfer_id": "11111111-1111-1111-1111-111111111111",
            "file_name": "photo.jpg",
            "file_size": 1024,
            "mime_type": "image/jpeg",
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "chunk_size": 65536,
            "chunk_count": 1,
            "sender_identity": "abc",
            "created_at": 1234567890
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_01_on_transfer_request_notifies_ui(self):
        listener = MagicMock()
        self.service.add_transfer_request_listener(listener)
        
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        
        self.assertIn(self.valid_meta_dict["transfer_id"], self.service._incoming_transfers)
        listener.assert_called_once_with(
            "192.168.1.100:5000", 
            self.valid_meta_dict["transfer_id"], 
            "photo.jpg", 
            1024
        )
        self.service.send_encrypted.assert_not_called()  # Phase 3B waits for user approval

    async def test_02_accept_transfer(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)
        
        self.assertEqual(self.service._incoming_transfers[transfer_id].state, TransferState.TRANSFERRING)
        self.service.send_encrypted.assert_called_with(
            self.ps, MessageType.TRANSFER_ACCEPT, {"transfer_id": transfer_id}
        )

    async def test_03_reject_transfer(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        
        await self.service.reject_transfer("192.168.1.100:5000", transfer_id)
        
        self.assertNotIn(transfer_id, self.service._incoming_transfers)
        self.service.send_encrypted.assert_called_with(
            self.ps, MessageType.TRANSFER_REJECT, {"transfer_id": transfer_id, "reason": "USER_REJECTED"}
        )

    async def test_04_transfer_request_invalid_metadata(self):
        invalid_meta = self.valid_meta_dict.copy()
        invalid_meta["file_size"] = -100
        
        await self.service._on_transfer_request(self.ps, invalid_meta)
        
        self.service.send_encrypted.assert_called_once()
        args, kwargs = self.service.send_encrypted.call_args
        self.assertEqual(args[1], MessageType.TRANSFER_ERROR)
        self.assertEqual(args[2]["error_code"], "INVALID_REQUEST")
        self.assertIn(invalid_meta["transfer_id"], args[2]["transfer_id"])

    async def test_05_transfer_request_busy(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        
        second_meta = self.valid_meta_dict.copy()
        second_meta["transfer_id"] = "22222222-2222-2222-2222-222222222222"
        await self.service._on_transfer_request(self.ps, second_meta)
        
        self.service.send_encrypted.assert_called_once_with(
            self.ps, MessageType.TRANSFER_REJECT, 
            {"transfer_id": "22222222-2222-2222-2222-222222222222", "reason": "BUSY"}
        )

    async def test_06_on_transfer_complete_success(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)
        
        incoming = self.service._incoming_transfers.get(transfer_id)
        # Mock finalise to return True (success)
        with patch.object(incoming, "finalise", return_value=True):
            await self.service._on_transfer_complete(self.ps, transfer_id)
            
        self.assertNotIn(transfer_id, self.service._incoming_transfers)
        self.service.send_encrypted.assert_called_with(
            self.ps, MessageType.TRANSFER_RESULT,
            {"transfer_id": transfer_id, "success": True, "sha256": self.valid_meta_dict["sha256"]}
        )

    async def test_07_on_transfer_complete_failure(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)
        
        incoming = self.service._incoming_transfers.get(transfer_id)
        # Mock finalise to return False (failure)
        with patch.object(incoming, "finalise", return_value=False):
            await self.service._on_transfer_complete(self.ps, transfer_id)
            
        self.service.send_encrypted.assert_called_with(
            self.ps, MessageType.TRANSFER_RESULT,
            {"transfer_id": transfer_id, "success": False, "sha256": ""}
        )

    async def test_08_on_transfer_cancel(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        
        await self.service._on_transfer_cancel(self.ps, transfer_id, "ABORTED")
        self.assertNotIn(transfer_id, self.service._incoming_transfers)

    async def test_09_on_transfer_error(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        
        env = FerryEnvelope(type="TRANSFER_ERROR", payload={"transfer_id": transfer_id, "error_code": "FAIL"})
        await self.service._handle_transfer_message(self.ps, env)
        self.assertNotIn(transfer_id, self.service._incoming_transfers)

    async def test_10_transfer_chunk_routing(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)
        
        incoming = self.service._incoming_transfers.get(transfer_id)
        with patch.object(incoming, "receive_chunk") as mock_receive:
            chunk_bytes = encode_chunk_frame(transfer_id, 0, b"data")
            await self.service._on_chunk_received(chunk_bytes)
            mock_receive.assert_called_once()
            args, kwargs = mock_receive.call_args
            self.assertEqual(args[0].seq, 0)
            self.assertEqual(args[0].data, b"data")

    async def test_11_transfer_chunk_invalid(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)
        
        incoming = self.service._incoming_transfers.get(transfer_id)
        with patch.object(incoming, "receive_chunk") as mock_receive:
            # Bad magic
            await self.service._on_chunk_received(b"BADM" + b"\x00"*24)
            mock_receive.assert_not_called()

    async def test_12_transfer_chunk_unknown_id(self):
        unknown_id = "99999999-9999-9999-9999-999999999999"
        chunk_bytes = encode_chunk_frame(unknown_id, 0, b"data")
        # Should not crash, just logs a warning
        await self.service._on_chunk_received(chunk_bytes)

    async def test_13_storage_collision_appends_suffix(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)
        
        incoming = self.service._incoming_transfers.get(transfer_id)
        incoming.meta.sha256 = "6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b" # sha256 of b'1'
        
        # Create dummy file to cause collision
        dest = Path(self.temp_dir) / "photo.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.touch()
        
        chunk = encode_chunk_frame(transfer_id, 0, b"1")
        await self.service._on_chunk_received(chunk)
        
        self.assertTrue(incoming.finalise())
        
        # Check if it was renamed to <prefix>_photo.jpg
        expected_dest = dest.parent / f"{transfer_id[:8]}_photo.jpg"
        self.assertTrue(expected_dest.exists())
        self.assertTrue(dest.exists()) # Original file should still be there

    async def test_14_path_traversal_sanitised(self):
        meta = self.valid_meta_dict.copy()
        meta["file_name"] = "../../../etc/passwd"
        
        await self.service._on_transfer_request(self.ps, meta)
        
        # It shouldn't send an error, it should just sanitise and proceed to ask user
        self.service.send_encrypted.assert_not_called()
        self.assertIn(meta["transfer_id"], self.service._incoming_transfers)
        
        # The stored incoming transfer should have the sanitised name
        incoming = self.service._incoming_transfers[meta["transfer_id"]]
        self.assertEqual(incoming.meta.file_name, "passwd")

    async def test_15_file_size_limit(self):
        meta = self.valid_meta_dict.copy()
        meta["file_size"] = 11 * 1024 ** 3 # 11 GiB
        
        await self.service._on_transfer_request(self.ps, meta)
        args, kwargs = self.service.send_encrypted.call_args
        self.assertEqual(args[1], MessageType.TRANSFER_ERROR)
        self.assertIn("limit", args[2]["message"])

    async def test_16_chunk_size_limit(self):
        meta = self.valid_meta_dict.copy()
        meta["chunk_size"] = 10000000 
        
        await self.service._on_transfer_request(self.ps, meta)
        args, kwargs = self.service.send_encrypted.call_args
        self.assertEqual(args[1], MessageType.TRANSFER_ERROR)
        self.assertIn("out of range", args[2]["message"])

    async def test_17_missing_chunk_count_mismatch(self):
        meta = self.valid_meta_dict.copy()
        meta["chunk_count"] = 5 # Should be 1
        
        await self.service._on_transfer_request(self.ps, meta)
        args, kwargs = self.service.send_encrypted.call_args
        self.assertEqual(args[1], MessageType.TRANSFER_ERROR)
        self.assertIn("chunk_count", args[2]["message"])

    async def test_18_reserved_filename_rejected(self):
        meta = self.valid_meta_dict.copy()
        meta["file_name"] = "CON.txt"
        
        await self.service._on_transfer_request(self.ps, meta)
        args, kwargs = self.service.send_encrypted.call_args
        self.assertEqual(args[1], MessageType.TRANSFER_ERROR)
        self.assertIn("Empty or invalid file_name", args[2]["message"])

    async def test_19_transfer_listener_exception_caught(self):
        def bad_listener(*args):
            raise RuntimeError("Boom!")
            
        self.service.add_transfer_request_listener(bad_listener)
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        # Should not raise an exception because it's caught
        self.assertIn(self.valid_meta_dict["transfer_id"], self.service._incoming_transfers)

    async def test_20_incomplete_transfer_cleanup_on_cancel(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)
        
        incoming = self.service._incoming_transfers.get(transfer_id)
        temp_path = incoming._temp_path
        self.assertTrue(temp_path.exists())
        
        await self.service._on_transfer_cancel(self.ps, transfer_id, "CANCEL")
        self.assertFalse(temp_path.exists())

    async def test_21_cancel_transfer_locally_sends_cancel_and_cleans_up(self):
        await self.service._on_transfer_request(self.ps, self.valid_meta_dict)
        transfer_id = self.valid_meta_dict["transfer_id"]
        await self.service.accept_transfer("192.168.1.100:5000", transfer_id)

        incoming = self.service._incoming_transfers.get(transfer_id)
        temp_path = incoming._temp_path
        self.assertTrue(temp_path.exists())

        cancelled = await self.service.cancel_transfer(transfer_id)
        self.assertTrue(cancelled)
        self.assertNotIn(transfer_id, self.service._incoming_transfers)
        self.assertFalse(temp_path.exists())
        self.service.send_encrypted.assert_called_with(
            self.ps, MessageType.TRANSFER_CANCEL,
            {"transfer_id": transfer_id, "reason": "USER_CANCELLED"}
        )

if __name__ == '__main__':
    unittest.main()
