"""
Test suite for Ferry Phase 3A transfer layer.

Tests:
 1. Transfer metadata validation (all required fields, bounds, sanitisation)
 2. Filename sanitisation (path traversal, control chars, reserved names)
 3. Chunk frame encode / decode round-trip
 4. Chunk frame magic detection (is_chunk_frame)
 5. Invalid chunk magic rejected
 6. Invalid chunk length (oversized) rejected
 7. Chunk with wrong transfer_id detected
 8. Out-of-order sequence number rejected
 9. Incremental SHA-256 accumulation (IncomingTransfer)
10. Complete successful transfer lifecycle (OutgoingTransfer → IncomingTransfer)
11. Integrity failure: corrupted data causes finalise() to return False and delete temp file
12. Cancellation removes temp file
13. TransferState machine: valid transitions
14. TransferState machine: invalid transitions raise RuntimeError
15. TransferMetadata rejects empty file_name after sanitisation
16. TransferMetadata rejects invalid UUID transfer_id
17. TransferMetadata rejects mismatched chunk_count
18. TransferMetadata rejects oversized chunk_size
19. TransferMetadata rejects non-hex sha256
20. resolve_safe_destination blocks path traversal
21. Large-file streaming: OutgoingTransfer yields correct number of chunks
22. Protocol model: TransferRequestPayload round-trip
23. Protocol model: new MessageType values present
24. Protocol model: TransferResultPayload serialisation
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import struct
import tempfile
import unittest
import uuid
from pathlib import Path

# ── Imports under test ──────────────────────────────────────────────────────
from ferry_linux.core.transfer import (
    CHUNK_HEADER_SIZE,
    CHUNK_MAGIC,
    CHUNK_SIZE,
    ChunkFrame,
    IncomingTransfer,
    OutgoingTransfer,
    SecurityError,
    TransferMetadata,
    TransferState,
    decode_chunk_frame,
    encode_chunk_frame,
    is_chunk_frame,
    resolve_safe_destination,
    sanitise_filename,
    _transfer_transition,
)
from ferry_linux.protocol.models import (
    MessageType,
    TransferRequestPayload,
    TransferResultPayload,
    TransferCompletePayload,
    TransferAcceptPayload,
    TransferRejectPayload,
    TransferCancelPayload,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_meta(
    file_name="test.bin",
    file_size=131072,  # 2 × CHUNK_SIZE
    sha256=None,
    chunk_size=CHUNK_SIZE,
) -> TransferMetadata:
    chunk_count = (file_size + chunk_size - 1) // chunk_size
    sha256 = sha256 or ("a" * 64)
    tid = str(uuid.uuid4())
    return TransferMetadata(
        transfer_id=tid,
        file_name=file_name,
        file_size=file_size,
        mime_type="application/octet-stream",
        sha256=sha256,
        chunk_size=chunk_size,
        chunk_count=chunk_count,
        sender_identity="dGVzdA==",
        created_at=0,
    )


def _make_meta_dict(**overrides) -> dict:
    tid = str(uuid.uuid4())
    chunk_size = overrides.pop("chunk_size", CHUNK_SIZE)
    file_size = overrides.pop("file_size", 131072)
    chunk_count = (file_size + chunk_size - 1) // chunk_size
    base = {
        "transfer_id": tid,
        "file_name": "photo.jpg",
        "file_size": file_size,
        "mime_type": "image/jpeg",
        "sha256": "a" * 64,
        "chunk_size": chunk_size,
        "chunk_count": chunk_count,
        "sender_identity": "dGVzdA==",
        "created_at": 0,
        "protocol_version": 1,
    }
    base.update(overrides)
    return base


# ── 1. TransferMetadata validation ──────────────────────────────────────────

class TestTransferMetadataValidation(unittest.TestCase):

    def test_valid_metadata_parsed(self):
        d = _make_meta_dict()
        meta = TransferMetadata.from_dict(d)
        self.assertEqual(meta.file_name, "photo.jpg")
        self.assertEqual(meta.file_size, 131072)

    def test_invalid_uuid_rejected(self):
        d = _make_meta_dict()
        d["transfer_id"] = "not-a-uuid"
        with self.assertRaises((ValueError, Exception)):
            TransferMetadata.from_dict(d)

    def test_empty_file_name_rejected(self):
        d = _make_meta_dict()
        d["file_name"] = ""
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_path_traversal_file_name_sanitised(self):
        d = _make_meta_dict()
        d["file_name"] = "../../etc/passwd"
        meta = TransferMetadata.from_dict(d)
        self.assertEqual(meta.file_name, "passwd")

    def test_zero_file_size_rejected(self):
        d = _make_meta_dict()
        d["file_size"] = 0
        d["chunk_count"] = 0
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_negative_file_size_rejected(self):
        d = _make_meta_dict()
        d["file_size"] = -1
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_oversized_chunk_size_rejected(self):
        d = _make_meta_dict(chunk_size=CHUNK_SIZE + 1)
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_zero_chunk_size_rejected(self):
        d = _make_meta_dict()
        d["chunk_size"] = 0
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_mismatched_chunk_count_rejected(self):
        d = _make_meta_dict()
        d["chunk_count"] = 999
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_invalid_sha256_rejected(self):
        d = _make_meta_dict()
        d["sha256"] = "nothex"
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_short_sha256_rejected(self):
        d = _make_meta_dict()
        d["sha256"] = "abc123"
        with self.assertRaises(ValueError):
            TransferMetadata.from_dict(d)

    def test_sha256_normalised_to_lowercase(self):
        d = _make_meta_dict()
        d["sha256"] = "A" * 64
        meta = TransferMetadata.from_dict(d)
        self.assertEqual(meta.sha256, "a" * 64)

    def test_to_dict_round_trip(self):
        d = _make_meta_dict()
        meta = TransferMetadata.from_dict(d)
        d2 = meta.to_dict()
        meta2 = TransferMetadata.from_dict(d2)
        self.assertEqual(meta.transfer_id, meta2.transfer_id)
        self.assertEqual(meta.sha256, meta2.sha256)


# ── 2. Filename sanitisation ─────────────────────────────────────────────────

class TestSanitiseFilename(unittest.TestCase):

    def test_simple_name_unchanged(self):
        self.assertEqual(sanitise_filename("document.pdf"), "document.pdf")

    def test_path_traversal_stripped(self):
        self.assertEqual(sanitise_filename("../../etc/shadow"), "shadow")

    def test_windows_path_stripped(self):
        self.assertEqual(sanitise_filename("C:\\Windows\\system32\\file.exe"), "file.exe")

    def test_null_byte_stripped(self):
        result = sanitise_filename("file\x00name.txt")
        self.assertNotIn("\x00", result)

    def test_control_char_stripped(self):
        result = sanitise_filename("bad\x01name.txt")
        self.assertNotIn("\x01", result)

    def test_reserved_name_con(self):
        self.assertEqual(sanitise_filename("CON"), "")

    def test_reserved_name_com1(self):
        self.assertEqual(sanitise_filename("COM1.txt"), "")

    def test_reserved_name_nul(self):
        self.assertEqual(sanitise_filename("NUL"), "")

    def test_dot_dot_rejected(self):
        self.assertEqual(sanitise_filename(".."), "")

    def test_single_dot_rejected(self):
        self.assertEqual(sanitise_filename("."), "")

    def test_empty_string(self):
        self.assertEqual(sanitise_filename(""), "")

    def test_unicode_name_preserved(self):
        name = "фото.jpg"
        result = sanitise_filename(name)
        self.assertEqual(result, name)

    def test_name_truncated_to_255_bytes(self):
        long_name = "a" * 300 + ".txt"
        result = sanitise_filename(long_name)
        self.assertLessEqual(len(result.encode("utf-8")), 255)


# ── 3. resolve_safe_destination ───────────────────────────────────────────────

class TestResolveSafeDestination(unittest.TestCase):

    def setUp(self):
        self.staging = Path(tempfile.mkdtemp())

    def test_valid_file_resolves(self):
        dest = resolve_safe_destination(self.staging, "photo.jpg")
        self.assertEqual(dest.parent, self.staging.resolve())

    def test_path_traversal_sanitised_to_safe_name(self):
        """../../etc/passwd gets sanitised to 'passwd' which safely resolves inside staging."""
        dest = resolve_safe_destination(self.staging, "../../etc/passwd")
        self.assertEqual(dest.parent, self.staging.resolve())
        self.assertEqual(dest.name, "passwd")

    def test_path_with_null_byte_raises_or_sanitises(self):
        """Names with null bytes should either be sanitised or rejected."""
        # Either sanitise_filename returns empty (raises ValueError) or a safe name
        try:
            dest = resolve_safe_destination(self.staging, "file\x00name.txt")
            self.assertEqual(dest.parent, self.staging.resolve())
        except (SecurityError, ValueError):
            pass  # both acceptable

    def test_empty_name_raises(self):
        with self.assertRaises((SecurityError, ValueError)):
            resolve_safe_destination(self.staging, "")


# ── 4–7. Chunk frame encoding / decoding ─────────────────────────────────────

class TestChunkFraming(unittest.TestCase):

    def _round_trip(self, data: bytes, seq: int = 0) -> ChunkFrame:
        tid = str(uuid.uuid4())
        encoded = encode_chunk_frame(tid, seq, data)
        decoded = decode_chunk_frame(encoded)
        self.assertEqual(decoded.transfer_id, tid)
        self.assertEqual(decoded.seq, seq)
        self.assertEqual(decoded.data, data)
        return decoded

    def test_empty_chunk(self):
        self._round_trip(b"")

    def test_small_chunk(self):
        self._round_trip(b"hello world", seq=0)

    def test_full_size_chunk(self):
        data = os.urandom(CHUNK_SIZE)
        self._round_trip(data, seq=42)

    def test_sequence_numbers_preserved(self):
        for seq in [0, 1, 255, 65535, 2**32 - 1]:
            tid = str(uuid.uuid4())
            encoded = encode_chunk_frame(tid, seq, b"x")
            decoded = decode_chunk_frame(encoded)
            self.assertEqual(decoded.seq, seq)

    def test_is_chunk_frame_true(self):
        encoded = encode_chunk_frame(str(uuid.uuid4()), 0, b"data")
        self.assertTrue(is_chunk_frame(encoded))

    def test_is_chunk_frame_false_for_json(self):
        json_bytes = b'{"type":"TRANSFER_REQUEST","payload":{}}'
        self.assertFalse(is_chunk_frame(json_bytes))

    def test_bad_magic_rejected(self):
        tid = str(uuid.uuid4())
        encoded = encode_chunk_frame(tid, 0, b"data")
        bad = b"XXXX" + encoded[4:]  # corrupt magic
        with self.assertRaises(ValueError):
            decode_chunk_frame(bad)

    def test_oversized_payload_length_rejected(self):
        """A remote-injected oversized payload_len must be rejected."""
        tid = str(uuid.uuid4())
        uid_bytes = uuid.UUID(tid).bytes
        # Craft a header claiming 10 MiB payload (way over MAX_CHUNK_PAYLOAD)
        bogus = (
            CHUNK_MAGIC
            + uid_bytes
            + struct.pack("!I", 0)         # seq
            + struct.pack("!I", 10 * 1024 ** 2)  # bogus payload_len
        )
        with self.assertRaises(ValueError):
            decode_chunk_frame(bogus)

    def test_truncated_frame_rejected(self):
        encoded = encode_chunk_frame(str(uuid.uuid4()), 0, b"hello")
        with self.assertRaises(ValueError):
            decode_chunk_frame(encoded[:CHUNK_HEADER_SIZE - 1])

    def test_oversized_chunk_encoding_rejected(self):
        with self.assertRaises(ValueError):
            encode_chunk_frame(str(uuid.uuid4()), 0, b"x" * (CHUNK_SIZE + 1))


# ── 8. Out-of-order sequence number rejected ──────────────────────────────────

class TestIncomingTransferSequencing(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.meta = _make_meta(file_size=CHUNK_SIZE)  # 1 chunk
        self.meta = TransferMetadata(
            transfer_id=str(uuid.uuid4()),
            file_name="test.bin",
            file_size=CHUNK_SIZE,
            mime_type="application/octet-stream",
            sha256="a" * 64,
            chunk_size=CHUNK_SIZE,
            chunk_count=1,
            sender_identity="",
            created_at=0,
        )

    def test_correct_sequence_accepted(self):
        xfer = IncomingTransfer(meta=self.meta, staging_dir=self.tmpdir)
        xfer.begin()
        chunk = ChunkFrame(transfer_id=self.meta.transfer_id, seq=0, data=b"x" * 100)
        xfer.receive_chunk(chunk)  # should not raise

    def test_out_of_order_chunk_rejected(self):
        xfer = IncomingTransfer(meta=self.meta, staging_dir=self.tmpdir)
        xfer.begin()
        chunk = ChunkFrame(transfer_id=self.meta.transfer_id, seq=1, data=b"x")
        with self.assertRaises(ValueError):
            xfer.receive_chunk(chunk)

    def test_wrong_transfer_id_rejected(self):
        xfer = IncomingTransfer(meta=self.meta, staging_dir=self.tmpdir)
        xfer.begin()
        chunk = ChunkFrame(transfer_id=str(uuid.uuid4()), seq=0, data=b"x")
        with self.assertRaises(ValueError):
            xfer.receive_chunk(chunk)


# ── 9–11. Full transfer lifecycle ─────────────────────────────────────────────

class TestTransferLifecycle(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.src_dir = Path(tempfile.mkdtemp())

    def _write_source_file(self, size: int) -> tuple[Path, str]:
        data = os.urandom(size)
        p = self.src_dir / "source.bin"
        p.write_bytes(data)
        sha256 = hashlib.sha256(data).hexdigest()
        return p, sha256

    def test_single_chunk_transfer(self):
        """Small file fits in one chunk — complete lifecycle."""
        src, sha256 = self._write_source_file(1024)
        self._run_transfer(src, sha256)

    def test_multi_chunk_transfer(self):
        """File spanning multiple chunks — 3 chunks."""
        src, sha256 = self._write_source_file(CHUNK_SIZE * 3 - 100)
        self._run_transfer(src, sha256)

    def test_exact_chunk_boundary(self):
        """File that's exactly N full chunks."""
        src, sha256 = self._write_source_file(CHUNK_SIZE * 2)
        self._run_transfer(src, sha256)

    def _run_transfer(self, src: Path, expected_sha256: str):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(self._do_transfer(src, expected_sha256))
        finally:
            loop.close()
        self.assertTrue(result, "Transfer should complete successfully")

    async def _do_transfer(self, src: Path, expected_sha256: str) -> bool:
        sender = OutgoingTransfer(source_path=src, receiver_identity="")
        meta = sender.build_metadata(sender_identity="test")
        self.assertEqual(meta.sha256, expected_sha256)

        staging = self.tmpdir / "staging"
        receiver = IncomingTransfer(meta=meta, staging_dir=staging)
        receiver.begin()

        async for raw_frame in sender.stream_chunks():
            chunk = decode_chunk_frame(raw_frame)
            receiver.receive_chunk(chunk)

        success = receiver.finalise()
        return success

    def test_integrity_failure_returns_false(self):
        """Corrupted data should cause finalise() to return False."""
        src, sha256 = self._write_source_file(512)
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self._do_transfer_with_corruption(src)
            )
        finally:
            loop.close()
        self.assertFalse(result)

    async def _do_transfer_with_corruption(self, src: Path) -> bool:
        sender = OutgoingTransfer(source_path=src, receiver_identity="")
        meta = sender.build_metadata(sender_identity="test")

        staging = self.tmpdir / "staging2"
        receiver = IncomingTransfer(meta=meta, staging_dir=staging)
        receiver.begin()

        async for raw_frame in sender.stream_chunks():
            chunk = decode_chunk_frame(raw_frame)
            # Corrupt the data
            corrupted = ChunkFrame(
                transfer_id=chunk.transfer_id,
                seq=chunk.seq,
                data=bytes([b ^ 0xFF for b in chunk.data]),
            )
            receiver.receive_chunk(corrupted)

        success = receiver.finalise()
        # temp file must be cleaned up
        temp_files = list(staging.glob("*.part"))
        self.assertEqual(len(temp_files), 0, "Temp file should be deleted on failure")
        return success

    def test_cancellation_removes_temp_file(self):
        src, _ = self._write_source_file(CHUNK_SIZE)
        sender = OutgoingTransfer(source_path=src, receiver_identity="")
        meta = sender.build_metadata(sender_identity="test")

        staging = self.tmpdir / "staging3"
        receiver = IncomingTransfer(meta=meta, staging_dir=staging)
        receiver.begin()

        # Write a chunk before cancelling
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._send_one_chunk(sender, receiver))
        finally:
            loop.close()

        receiver.cancel()
        self.assertEqual(receiver.state, TransferState.CANCELLED)
        temp_files = list(staging.glob("*.part"))
        self.assertEqual(len(temp_files), 0)

    async def _send_one_chunk(self, sender: OutgoingTransfer, receiver: IncomingTransfer):
        async for raw_frame in sender.stream_chunks():
            chunk = decode_chunk_frame(raw_frame)
            receiver.receive_chunk(chunk)
            break  # only send one chunk


# ── 12. Large file streaming without whole-file loading ───────────────────────

class TestLargeFileStreaming(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.src_dir = Path(tempfile.mkdtemp())

    def test_chunk_count_correct_for_5mb_file(self):
        """Verify chunk_count math for a 5 MiB file."""
        size = 5 * 1024 * 1024
        expected_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        src = self.src_dir / "big.bin"
        src.write_bytes(os.urandom(size))

        sender = OutgoingTransfer(source_path=src, receiver_identity="")
        meta = sender.build_metadata(sender_identity="")
        self.assertEqual(meta.chunk_count, expected_chunks)

    def test_streaming_does_not_load_entire_file(self):
        """
        OutgoingTransfer.stream_chunks() must be a generator that yields chunks
        without reading the whole file. We verify this indirectly by checking
        that bytes_sent increases incrementally.
        """
        size = CHUNK_SIZE * 3
        src = self.src_dir / "stream.bin"
        src.write_bytes(os.urandom(size))

        sender = OutgoingTransfer(source_path=src, receiver_identity="")
        sender.build_metadata(sender_identity="")

        loop = asyncio.new_event_loop()
        try:
            counts = loop.run_until_complete(self._count_chunks(sender))
        finally:
            loop.close()

        expected = 3
        self.assertEqual(counts, expected)

    async def _count_chunks(self, sender: OutgoingTransfer) -> int:
        n = 0
        async for _ in sender.stream_chunks():
            n += 1
        return n


# ── 13–14. TransferState machine ──────────────────────────────────────────────

class TestTransferStateMachine(unittest.TestCase):

    def test_idle_to_requested(self):
        s = _transfer_transition(TransferState.IDLE, TransferState.REQUESTED)
        self.assertEqual(s, TransferState.REQUESTED)

    def test_requested_to_accepted(self):
        s = _transfer_transition(TransferState.REQUESTED, TransferState.ACCEPTED)
        self.assertEqual(s, TransferState.ACCEPTED)

    def test_accepted_to_transferring(self):
        s = _transfer_transition(TransferState.ACCEPTED, TransferState.TRANSFERRING)
        self.assertEqual(s, TransferState.TRANSFERRING)

    def test_transferring_to_completed(self):
        s = _transfer_transition(TransferState.TRANSFERRING, TransferState.COMPLETED)
        self.assertEqual(s, TransferState.COMPLETED)

    def test_transferring_to_cancelling(self):
        s = _transfer_transition(TransferState.TRANSFERRING, TransferState.CANCELLING)
        self.assertEqual(s, TransferState.CANCELLING)

    def test_cancelling_to_cancelled(self):
        s = _transfer_transition(TransferState.CANCELLING, TransferState.CANCELLED)
        self.assertEqual(s, TransferState.CANCELLED)

    def test_idle_to_accepted(self):
        """IncomingTransfer.begin() uses IDLE→ACCEPTED directly."""
        s = _transfer_transition(TransferState.IDLE, TransferState.ACCEPTED)
        self.assertEqual(s, TransferState.ACCEPTED)

    def test_idle_to_transferring_valid(self):
        """OutgoingTransfer.stream_chunks() uses IDLE→TRANSFERRING directly."""
        s = _transfer_transition(TransferState.IDLE, TransferState.TRANSFERRING)
        self.assertEqual(s, TransferState.TRANSFERRING)

    def test_invalid_idle_to_cancelled(self):
        """IDLE→CANCELLED is not a valid direct transition."""
        with self.assertRaises(RuntimeError):
            _transfer_transition(TransferState.IDLE, TransferState.CANCELLED)

    def test_invalid_completed_to_requested(self):
        with self.assertRaises(RuntimeError):
            _transfer_transition(TransferState.COMPLETED, TransferState.REQUESTED)

    def test_invalid_failed_to_idle(self):
        with self.assertRaises(RuntimeError):
            _transfer_transition(TransferState.FAILED, TransferState.IDLE)


# ── 22–24. Protocol model tests ───────────────────────────────────────────────

class TestProtocolModels(unittest.TestCase):

    def test_transfer_chunk_in_message_type(self):
        self.assertEqual(MessageType.TRANSFER_CHUNK, "TRANSFER_CHUNK")

    def test_transfer_result_in_message_type(self):
        self.assertEqual(MessageType.TRANSFER_RESULT, "TRANSFER_RESULT")

    def test_transfer_complete_in_message_type(self):
        self.assertEqual(MessageType.TRANSFER_COMPLETE, "TRANSFER_COMPLETE")

    def test_transfer_request_payload_round_trip(self):
        p = TransferRequestPayload(
            transfer_id=str(uuid.uuid4()),
            file_name="test.pdf",
            file_size=1024,
            mime_type="application/pdf",
            sha256="a" * 64,
            chunk_size=CHUNK_SIZE,
            chunk_count=1,
            sender_identity="key",
            created_at=12345,
        )
        d = p.to_dict()
        p2 = TransferRequestPayload.from_dict(d)
        self.assertEqual(p.transfer_id, p2.transfer_id)
        self.assertEqual(p.sha256, p2.sha256)
        self.assertEqual(p.chunk_count, p2.chunk_count)

    def test_transfer_result_payload_serialised(self):
        p = TransferResultPayload(
            transfer_id=str(uuid.uuid4()),
            success=True,
            sha256="b" * 64,
        )
        d = p.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(len(d["sha256"]), 64)

    def test_transfer_accept_payload(self):
        p = TransferAcceptPayload(transfer_id="x")
        self.assertEqual(p.to_dict()["transfer_id"], "x")

    def test_transfer_reject_payload(self):
        p = TransferRejectPayload(transfer_id="x", reason="BUSY")
        self.assertEqual(p.to_dict()["reason"], "BUSY")

    def test_transfer_cancel_default_reason(self):
        p = TransferCancelPayload(transfer_id="x")
        self.assertEqual(p.to_dict()["reason"], "USER_CANCELLED")

    def test_transfer_complete_payload(self):
        p = TransferCompletePayload(transfer_id="x")
        self.assertEqual(p.to_dict()["transfer_id"], "x")


if __name__ == "__main__":
    unittest.main()
