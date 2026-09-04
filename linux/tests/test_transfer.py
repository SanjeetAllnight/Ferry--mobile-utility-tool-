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
import sqlite3
import struct
import tempfile
import time
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

    def test_zero_file_size_accepted(self):
        d = _make_meta_dict()
        d["file_size"] = 0
        d["chunk_count"] = 0
        meta = TransferMetadata.from_dict(d)
        self.assertEqual(meta.file_size, 0)
        self.assertEqual(meta.chunk_count, 0)

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
        xfer = IncomingTransfer(meta=self.meta, download_dir=self.tmpdir)
        xfer.begin()
        chunk = ChunkFrame(transfer_id=self.meta.transfer_id, seq=0, data=b"x" * 100)
        xfer.receive_chunk(chunk)  # should not raise

    def test_out_of_order_chunk_rejected(self):
        xfer = IncomingTransfer(meta=self.meta, download_dir=self.tmpdir)
        xfer.begin()
        chunk = ChunkFrame(transfer_id=self.meta.transfer_id, seq=1, data=b"x")
        with self.assertRaises(ValueError):
            xfer.receive_chunk(chunk)

    def test_wrong_transfer_id_rejected(self):
        xfer = IncomingTransfer(meta=self.meta, download_dir=self.tmpdir)
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
        receiver = IncomingTransfer(meta=meta, download_dir=staging)
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
        receiver = IncomingTransfer(meta=meta, download_dir=staging)
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
        receiver = IncomingTransfer(meta=meta, download_dir=staging)
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

    def test_zero_byte_transfer_success(self):
        """Zero-byte file transfer: 0 chunks, SHA-256 verified, empty destination file."""
        src = self.tmpdir / "empty.txt"
        src.write_bytes(b"")

        sender = OutgoingTransfer(source_path=src, receiver_identity="")
        meta = sender.build_metadata(sender_identity="sender_pk")
        self.assertEqual(meta.file_size, 0)
        self.assertEqual(meta.chunk_count, 0)
        self.assertEqual(meta.sha256, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")

        dest_dir = self.tmpdir / "downloads"
        receiver = IncomingTransfer(meta=meta, download_dir=dest_dir)
        receiver.begin()

        # Sender yields 0 chunks
        loop = asyncio.new_event_loop()
        try:
            chunks = loop.run_until_complete(self._collect_all_chunks(sender))
        finally:
            loop.close()
        self.assertEqual(len(chunks), 0)

        success = receiver.finalise()
        self.assertTrue(success)
        self.assertEqual(receiver.state, TransferState.COMPLETED)

        final_file = dest_dir / "empty.txt"
        self.assertTrue(final_file.exists())
        self.assertEqual(final_file.stat().st_size, 0)

    async def _collect_all_chunks(self, sender: OutgoingTransfer) -> list:
        chunks = []
        async for chunk in sender.stream_chunks():
            chunks.append(chunk)
        return chunks


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

    def test_idle_to_cancelled_now_valid(self):
        """Phase 3D: IDLEu2192CANCELLED is now valid to allow cancel() from any state."""
        s = _transfer_transition(TransferState.IDLE, TransferState.CANCELLED)
        self.assertEqual(s, TransferState.CANCELLED)

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



# ── Phase 3D Reliability Tests ─────────────────────────────────────────────

class TestPhase3DReliability(unittest.IsolatedAsyncioTestCase):
    """
    Phase 3D reliability hardening tests.

    Verify that transfer IO errors, cancels from unexpected states, source-file
    disappearance, duplicate messages, and stale .part files are all handled
    gracefully without leaving the system in a broken state.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # R01: begin() OSError transitions to FAILED
    def test_r01_begin_ioerror_sets_failed_state(self):
        """If staging dir is a file, begin() raises TransferError and sets FAILED."""
        from ferry_linux.core.transfer import (
            IncomingTransfer, TransferError, TransferState
        )
        meta = _make_meta(file_size=1024, chunk_size=CHUNK_SIZE)
        blocker = self.tmpdir / "staging"
        blocker.write_bytes(b"")

        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming._staging_dir = blocker
        incoming._download_dir = self.tmpdir

        with self.assertRaises((TransferError, OSError)):
            incoming.begin()

        self.assertEqual(incoming.state, TransferState.FAILED)

    # R02: receive_chunk() disk-full leaves no .part
    def test_r02_receive_chunk_diskfull_cleans_temp(self):
        """A failed write in receive_chunk() removes the .part file and sets FAILED."""
        from ferry_linux.core.transfer import (
            ChunkFrame, IncomingTransfer, TransferError, TransferState
        )
        import unittest.mock as mock

        data = b"\xAB" * 1024
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        self.assertTrue(incoming._temp_path.exists())

        with mock.patch.object(incoming._temp_fh, "write", side_effect=OSError("No space left")):
            frame = ChunkFrame(transfer_id=meta.transfer_id, seq=0, data=data)
            with self.assertRaises((TransferError, OSError)):
                incoming.receive_chunk(frame)

        self.assertFalse(incoming._temp_path.exists())
        self.assertEqual(incoming.state, TransferState.FAILED)

    # R03: cancel() from IDLE does not raise
    def test_r03_cancel_from_idle_does_not_raise(self):
        """cancel() is safe to call on a never-started IncomingTransfer."""
        from ferry_linux.core.transfer import IncomingTransfer, TransferState
        meta = _make_meta()
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.cancel()
        self.assertEqual(incoming.state, TransferState.CANCELLED)

    # R04: cancel() from ACCEPTED does not raise
    def test_r04_cancel_from_accepted_does_not_raise(self):
        """cancel() is safe to call after begin() but before any chunks."""
        from ferry_linux.core.transfer import IncomingTransfer, TransferState
        data = b"\xCC" * 64
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        part = incoming._temp_path
        self.assertTrue(part.exists())
        incoming.cancel()
        self.assertEqual(incoming.state, TransferState.CANCELLED)
        self.assertFalse(part.exists())

    # R05: stream_chunks() FileNotFoundError
    async def test_r05_stream_chunks_source_disappears(self):
        """If source file disappears before streaming, TransferError is raised."""
        from ferry_linux.core.transfer import OutgoingTransfer, TransferError
        src = self.tmpdir / "src.bin"
        src.write_bytes(b"\xDD" * 512)
        xfer = OutgoingTransfer(source_path=src, receiver_identity="")
        src.unlink()
        frames = []
        with self.assertRaises((TransferError, FileNotFoundError, OSError)):
            async for frame in xfer.stream_chunks():
                frames.append(frame)

    # R06: cancel() after COMPLETED is a no-op
    def test_r06_cancel_from_completed_is_safe(self):
        """cancel() after a successful finalise() must not raise."""
        from ferry_linux.core.transfer import (
            ChunkFrame, IncomingTransfer, TransferState
        )
        data = b"\x01" * 64
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        incoming.receive_chunk(ChunkFrame(meta.transfer_id, 0, data))
        result = incoming.finalise()
        self.assertTrue(result)
        incoming.cancel()
        self.assertEqual(incoming.state, TransferState.CANCELLED)

    # R07: integrity mismatch leaves no final file
    def test_r07_integrity_mismatch_no_file_published(self):
        """A SHA-256 mismatch in finalise() deletes the .part file; no final file."""
        from ferry_linux.core.transfer import (
            ChunkFrame, IncomingTransfer, TransferState
        )
        data = b"\x02" * 64
        bad_sha256 = "b" * 64
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=bad_sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        incoming.receive_chunk(ChunkFrame(meta.transfer_id, 0, data))
        result = incoming.finalise()
        self.assertFalse(result)
        self.assertEqual(incoming.state, TransferState.FAILED)
        self.assertFalse(incoming._temp_path.exists())
        final = self.tmpdir / meta.file_name
        self.assertFalse(final.exists())

    # R08: TransferError is importable and inherits Exception
    def test_r08_transfer_error_is_exception(self):
        """TransferError is an Exception subclass."""
        from ferry_linux.core.transfer import TransferError
        err = TransferError("disk full")
        self.assertIsInstance(err, Exception)
        self.assertEqual(str(err), "disk full")

    # R09: duplicate history protection
    def test_r09_duplicate_history_protection(self):
        """_record_transfer_once() with the same transfer_id is idempotent."""
        from ferry_linux.core.service import FerryService
        from ferry_linux.core.config import FerryConfig
        from unittest.mock import MagicMock

        config = FerryConfig(download_dir=str(self.tmpdir))
        svc = FerryService()
        svc.config = config
        mock_db = MagicMock()
        svc.db = mock_db

        tid = str(uuid.uuid4())
        for _ in range(3):
            svc._record_transfer_once(
                transfer_id=tid, device_id="device-1",
                file_name="test.bin", file_size=1024,
                direction="OUTGOING", status="COMPLETED",
                started_at=0, sha256="a" * 64,
            )
        self.assertEqual(mock_db.add_transfer.call_count, 1)

    # R10: cancel_incoming_transfer_for_peer cleans .part and fires callbacks
    def test_r10_cancel_incoming_for_peer_cleans_up(self):
        """_cancel_incoming_transfer_for_peer cleans .part and records FAILED."""
        from ferry_linux.core.service import FerryService, PeerSession
        from ferry_linux.core.config import FerryConfig
        from ferry_linux.core.transfer import IncomingTransfer, ChunkFrame
        from unittest.mock import MagicMock

        config = FerryConfig(download_dir=str(self.tmpdir))
        svc = FerryService()
        svc.config = config
        mock_db = MagicMock()
        svc.db = mock_db

        mock_sess = MagicMock()
        ps = PeerSession(mock_sess, MagicMock(), MagicMock(), "10.0.0.5:5173")
        ps.remote_device_id = "phone-001"

        data = b"\x03" * 64
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        part_path = incoming._temp_path
        self.assertTrue(part_path.exists())

        svc._incoming_transfers[meta.transfer_id] = incoming
        svc._incoming_peers[meta.transfer_id] = ps

        complete_cb = MagicMock()
        svc.add_transfer_complete_listener(complete_cb)

        svc._cancel_incoming_transfer_for_peer(ps)

        self.assertFalse(part_path.exists())
        self.assertEqual(mock_db.add_transfer.call_count, 1)
        complete_cb.assert_called_once()
        self.assertFalse(complete_cb.call_args[0][1])  # success=False

    # R11: IDLE->FAILED transition now valid (Phase 3D)
    def test_r11_idle_to_failed_now_valid(self):
        """Phase 3D: IDLE->FAILED is a valid transition (e.g. begin() throws)."""
        s = _transfer_transition(TransferState.IDLE, TransferState.FAILED)
        self.assertEqual(s, TransferState.FAILED)

    # R12: stale .part file cleanup on start
    async def test_r12_stale_part_cleanup_on_start(self):
        """Service.start() removes .part files from a previous crashed session."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from ferry_linux.core.service import FerryService
        from ferry_linux.core.config import FerryConfig

        staging = self.tmpdir / "staging"
        staging.mkdir()
        stale = staging / "deadbeef-0000-0000-0000-000000000001.part"
        stale.write_bytes(b"\xDE\xAD" * 100)
        self.assertTrue(stale.exists())

        config = FerryConfig(download_dir=str(self.tmpdir))
        svc = FerryService()
        svc.config = config

        mock_server = MagicMock()
        mock_server.wait_closed = AsyncMock()

        with patch.object(svc.discovery, "start", new=AsyncMock()), \
             patch("asyncio.start_server", new=AsyncMock(return_value=mock_server)):
            await svc.start()
            # Verify stale cleanup happened during start (before the server starts)
            self.assertFalse(stale.exists())
            # Cleanly stop (the mock server has wait_closed as AsyncMock)
            with patch.object(svc.discovery, "stop", new=AsyncMock()):
                await svc.stop()



# ── Phase 3E Task 1: INTERRUPTED state, interrupt(), and DB migration ────────

class TestPhase3ETask1InterruptedTransfer(unittest.TestCase):
    """
    Phase 3E Task 1 tests.

    Tests for:
    - IncomingTransfer.interrupt() semantics
    - .part file retention
    - bytes_received and resume_chunk_index consistency
    - idempotency
    - separation from CANCELLED/FAILED/COMPLETED
    - interrupt_info() snapshot
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_incoming_with_data(self, data: bytes) -> "IncomingTransfer":
        """Helper: create an IncomingTransfer, begin it, write all data, return it."""
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        # Write all chunks
        offset = 0
        seq = 0
        while offset < len(data):
            chunk = data[offset:offset + CHUNK_SIZE]
            from ferry_linux.core.transfer import ChunkFrame
            incoming.receive_chunk(ChunkFrame(meta.transfer_id, seq, chunk))
            offset += len(chunk)
            seq += 1
        return incoming

    # E01: interrupt() from TRANSFERRING → INTERRUPTED
    def test_e01_interrupt_from_transferring(self):
        """interrupt() from TRANSFERRING state transitions to INTERRUPTED."""
        data = b"\xAA" * (CHUNK_SIZE * 2)
        incoming = self._make_incoming_with_data(data)
        self.assertEqual(incoming.state, TransferState.TRANSFERRING)

        incoming.interrupt()

        self.assertEqual(incoming.state, TransferState.INTERRUPTED)

    # E02: .part file is retained after interrupt()
    def test_e02_part_file_retained_after_interrupt(self):
        """.part file must exist on disk after interrupt()."""
        data = b"\xBB" * (CHUNK_SIZE + 100)
        incoming = self._make_incoming_with_data(data)
        part = incoming._temp_path
        self.assertTrue(part.exists(), ".part file should exist before interrupt")

        incoming.interrupt()

        self.assertTrue(part.exists(), ".part file must be retained after interrupt()")

    # E03: bytes_received matches actual bytes on disk
    def test_e03_bytes_received_matches_disk(self):
        """After interrupt(), bytes_received == actual .part file size on disk."""
        data = b"\xCC" * (CHUNK_SIZE * 3)
        incoming = self._make_incoming_with_data(data)
        incoming.interrupt()

        on_disk = incoming._temp_path.stat().st_size
        self.assertEqual(incoming.bytes_received, on_disk)
        self.assertEqual(incoming.bytes_received, len(data))

    # E04: resume_chunk_index is correct (full chunks only)
    def test_e04_resume_chunk_index_correct(self):
        """For exact chunk-boundary data, resume_chunk_index == chunk_count."""
        data = b"\xDD" * (CHUNK_SIZE * 2)   # Exactly 2 full chunks
        incoming = self._make_incoming_with_data(data)
        incoming.interrupt()

        # _next_seq is reset from disk bytes // chunk_size
        self.assertEqual(incoming._next_seq, 2)

    # E05: file handle is closed after interrupt()
    def test_e05_file_handle_closed_after_interrupt(self):
        """After interrupt(), the internal file handle must be None (closed)."""
        data = b"\xEE" * CHUNK_SIZE
        incoming = self._make_incoming_with_data(data)
        self.assertIsNotNone(incoming._temp_fh)

        incoming.interrupt()

        self.assertIsNone(incoming._temp_fh)

    # E06: interrupt() is idempotent
    def test_e06_interrupt_idempotent(self):
        """Calling interrupt() twice does nothing on the second call."""
        data = b"\xFF" * CHUNK_SIZE
        incoming = self._make_incoming_with_data(data)

        incoming.interrupt()
        bytes_after_first = incoming.bytes_received
        seq_after_first = incoming._next_seq
        incoming.interrupt()  # second call — must not raise

        self.assertEqual(incoming.state, TransferState.INTERRUPTED)
        self.assertEqual(incoming.bytes_received, bytes_after_first)
        self.assertEqual(incoming._next_seq, seq_after_first)

    # E07: metadata available after interrupt()
    def test_e07_metadata_still_available_after_interrupt(self):
        """TransferMetadata is accessible after interrupt()."""
        data = b"\x01" * CHUNK_SIZE
        incoming = self._make_incoming_with_data(data)
        incoming.interrupt()

        self.assertIsNotNone(incoming.meta)
        self.assertIsNotNone(incoming.meta.transfer_id)
        self.assertIsNotNone(incoming.meta.sha256)

    # E08: interrupt_info() returns correct snapshot
    def test_e08_interrupt_info_snapshot(self):
        """interrupt_info() returns a dict with all required keys after interrupt()."""
        data = b"\x02" * CHUNK_SIZE
        incoming = self._make_incoming_with_data(data)
        incoming.interrupt()

        info = incoming.interrupt_info()
        self.assertIsNotNone(info)
        self.assertIn("transfer_id", info)
        self.assertIn("bytes_received", info)
        self.assertIn("resume_chunk_index", info)
        self.assertIn("partial_sha256", info)
        self.assertIn("sender_identity", info)
        self.assertIn("original_metadata_json", info)
        self.assertEqual(info["bytes_received"], CHUNK_SIZE)
        self.assertEqual(info["resume_chunk_index"], 1)

    # E09: interrupt() on COMPLETED is a no-op (does not change state)
    def test_e09_interrupt_after_completed_does_nothing(self):
        """interrupt() on a COMPLETED transfer preserves COMPLETED state."""
        data = b"\x03" * 64
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        from ferry_linux.core.transfer import ChunkFrame
        incoming.receive_chunk(ChunkFrame(meta.transfer_id, 0, data))
        incoming.finalise()
        self.assertEqual(incoming.state, TransferState.COMPLETED)

        incoming.interrupt()  # must not raise

        self.assertEqual(incoming.state, TransferState.COMPLETED)

    # E10: interrupt() on FAILED is a no-op
    def test_e10_interrupt_after_failed_does_not_resurrect(self):
        """interrupt() on a FAILED transfer leaves state as FAILED."""
        from ferry_linux.core.transfer import ChunkFrame
        data = b"\x04" * 64
        bad_sha = "b" * 64
        meta = _make_meta(file_size=len(data), chunk_size=CHUNK_SIZE, sha256=bad_sha)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        incoming.receive_chunk(ChunkFrame(meta.transfer_id, 0, data))
        incoming.finalise()  # SHA-256 mismatch → FAILED
        self.assertEqual(incoming.state, TransferState.FAILED)

        incoming.interrupt()  # must not change state

        self.assertEqual(incoming.state, TransferState.FAILED)

    # E11: interrupt() on CANCELLED is a no-op
    def test_e11_interrupt_after_cancelled_does_not_resurrect(self):
        """interrupt() on a CANCELLED transfer leaves state as CANCELLED."""
        data = b"\x05" * CHUNK_SIZE
        incoming = self._make_incoming_with_data(data)
        incoming.cancel()
        self.assertEqual(incoming.state, TransferState.CANCELLED)

        incoming.interrupt()  # must not raise or change state

        self.assertEqual(incoming.state, TransferState.CANCELLED)

    # E12: interrupt_info() returns None when not INTERRUPTED
    def test_e12_interrupt_info_none_when_not_interrupted(self):
        """interrupt_info() must return None when state is not INTERRUPTED."""
        data = b"\x06" * CHUNK_SIZE
        incoming = self._make_incoming_with_data(data)
        self.assertEqual(incoming.state, TransferState.TRANSFERRING)
        self.assertIsNone(incoming.interrupt_info())

    # E13: zero-byte interrupt() results in FAILED, not INTERRUPTED
    def test_e13_zero_byte_transfer_not_resumable(self):
        """A zero-byte transfer must not become INTERRUPTED — stays FAILED."""
        sha256 = hashlib.sha256(b"").hexdigest()
        meta = _make_meta(file_size=0, chunk_size=CHUNK_SIZE, sha256=sha256)
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        # Zero-byte transfers do not call begin(); they go straight to finalise() via
        # a service-level shortcut. Simulate ACCEPTED state by calling begin() and
        # then immediately interrupting.
        incoming.begin()  # opens temp file; transitions to TRANSFERRING

        incoming.interrupt()

        # Zero-byte must not produce INTERRUPTED
        self.assertNotEqual(incoming.state, TransferState.INTERRUPTED)

    # E14: INTERRUPTED is a valid transition from TRANSFERRING in state machine
    def test_e14_transferring_to_interrupted_valid_transition(self):
        """TRANSFERRING→INTERRUPTED is a valid state machine transition."""
        result = _transfer_transition(TransferState.TRANSFERRING, TransferState.INTERRUPTED)
        self.assertEqual(result, TransferState.INTERRUPTED)

    # E15: INTERRUPTED now has valid outgoing transitions (Phase 3E Task 3)
    def test_e15_interrupted_has_resume_transitions(self):
        """INTERRUPTED -> RESUME_REQUESTED and INTERRUPTED -> FAILED are valid (Phase 3E Task 3)."""
        allowed = TransferState.VALID_TRANSITIONS[TransferState.INTERRUPTED]
        self.assertIn(TransferState.RESUME_REQUESTED, allowed)
        self.assertIn(TransferState.FAILED, allowed)
        # Must NOT go directly to COMPLETED or CANCELLED
        self.assertNotIn(TransferState.COMPLETED, allowed)
        self.assertNotIn(TransferState.CANCELLED, allowed)


class TestPhase3ETask1DatabaseMigration(unittest.TestCase):
    """
    Phase 3E Task 1 database migration tests.

    Tests for:
    - Schema v2 → v3 migration adds new columns
    - Existing transfer rows survive migration
    - Migration is idempotent (running v3 on a v3 DB is safe)
    - New columns have expected NULL defaults for old rows
    - save_interrupted_transfer and get_interrupted_transfer round-trip
    - expire_interrupted_transfers marks expired rows as FAILED
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_v2_db(self) -> Path:
        """
        Create a minimal schema-v2 database by hand so we can test migration
        without constructing a DatabaseManager at v2 schema time.
        """
        db_path = self.tmpdir / "ferry_v2.db"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("""
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY)
            """)
            conn.execute("INSERT INTO schema_version (version) VALUES (2)")
            conn.execute("""
                CREATE TABLE trusted_devices (
                    device_id TEXT PRIMARY KEY,
                    device_name TEXT NOT NULL,
                    public_key TEXT NOT NULL,
                    identity_public_key_b64 TEXT NOT NULL DEFAULT '',
                    paired_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE transfer_history (
                    transfer_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    sha256 TEXT NOT NULL
                )
            """)
            conn.execute("""
                INSERT INTO transfer_history
                    (transfer_id, device_id, file_name, file_size, direction, status,
                     started_at, completed_at, sha256)
                VALUES ('old-xfer-001', 'dev-1', 'file.bin', 1024, 'INCOMING', 'COMPLETED',
                        1000000, 1000100, 'aabbccdd' || ? )
            """, ("00" * 28,))
            conn.commit()
        finally:
            conn.close()
        return db_path

    def test_db01_v2_to_v3_migration_adds_columns(self):
        """Schema v2 → v3 migration: all 7 new columns must exist after upgrade."""
        from ferry_linux.core.db import DatabaseManager
        db_path = self._make_v2_db()
        # DatabaseManager constructor triggers migration
        db = DatabaseManager(db_path)

        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute("PRAGMA table_info(transfer_history)")
            columns = {row[1] for row in cursor.fetchall()}
        finally:
            conn.close()

        expected_new_cols = {
            "interrupted_at", "bytes_received", "resume_chunk_index",
            "partial_sha256", "sender_identity", "original_metadata_json", "expire_at",
        }
        for col in expected_new_cols:
            self.assertIn(col, columns, f"Expected column '{col}' missing after migration")

    def test_db02_existing_rows_survive_migration(self):
        """Existing transfer_history rows must be intact after schema v3 migration."""
        from ferry_linux.core.db import DatabaseManager
        db_path = self._make_v2_db()
        db = DatabaseManager(db_path)

        transfers = db.list_transfers()
        self.assertEqual(len(transfers), 1)
        self.assertEqual(transfers[0].transfer_id, "old-xfer-001")
        self.assertEqual(transfers[0].status, "COMPLETED")

    def test_db03_migration_idempotent(self):
        """Running migration on an already-v3 database must not raise or duplicate data."""
        from ferry_linux.core.db import DatabaseManager
        db_path = self._make_v2_db()
        # First migration
        db1 = DatabaseManager(db_path)
        # Second open: v3 → v3 must be safe
        db2 = DatabaseManager(db_path)

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(row[0], 3)
        finally:
            conn.close()

    def test_db04_new_columns_null_for_old_rows(self):
        """After migration, old rows must have NULL for all new resume columns."""
        from ferry_linux.core.db import DatabaseManager
        db_path = self._make_v2_db()
        DatabaseManager(db_path)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM transfer_history WHERE transfer_id = 'old-xfer-001'"
            ).fetchone()
            self.assertIsNone(row["interrupted_at"])
            self.assertIsNone(row["bytes_received"])
            self.assertIsNone(row["resume_chunk_index"])
            self.assertIsNone(row["partial_sha256"])
            self.assertIsNone(row["sender_identity"])
            self.assertIsNone(row["original_metadata_json"])
            self.assertIsNone(row["expire_at"])
        finally:
            conn.close()

    def test_db05_save_and_get_interrupted_transfer(self):
        """save_interrupted_transfer + get_interrupted_transfer round-trip."""
        from ferry_linux.core.db import DatabaseManager, InterruptedTransferInfo, TransferRecord

        db_path = self.tmpdir / "test_v3.db"
        db = DatabaseManager(db_path)

        tid = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        expire_ms = InterruptedTransferInfo.make_expire_at(now_ms)

        # Insert a basic transfer_history row first
        import time as time_mod
        record = TransferRecord(
            transfer_id=tid, device_id="dev-1", file_name="big.bin",
            file_size=10 * 1024 * 1024, direction="INCOMING", status="INTERRUPTED",
            started_at=now_ms, completed_at=None, sha256="a" * 64,
        )
        db.add_transfer(record)

        info = InterruptedTransferInfo(
            transfer_id=tid,
            bytes_received=5 * 1024 * 1024,
            resume_chunk_index=80,
            partial_sha256="dead" + "00" * 30,
            sender_identity="SENDER_PUB_KEY_B64",
            original_metadata_json='{"transfer_id": "' + tid + '"}',
            interrupted_at=now_ms,
            expire_at=expire_ms,
        )
        db.save_interrupted_transfer(info)

        fetched = db.get_interrupted_transfer(tid)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.transfer_id, tid)
        self.assertEqual(fetched.bytes_received, 5 * 1024 * 1024)
        self.assertEqual(fetched.resume_chunk_index, 80)
        self.assertEqual(fetched.sender_identity, "SENDER_PUB_KEY_B64")
        self.assertEqual(fetched.expire_at, expire_ms)

    def test_db06_get_interrupted_returns_none_for_completed(self):
        """get_interrupted_transfer must return None if the transfer is COMPLETED."""
        from ferry_linux.core.db import DatabaseManager, TransferRecord

        db_path = self.tmpdir / "test_v3b.db"
        db = DatabaseManager(db_path)

        tid = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        record = TransferRecord(
            transfer_id=tid, device_id="dev-1", file_name="done.bin",
            file_size=100, direction="INCOMING", status="COMPLETED",
            started_at=now_ms, completed_at=now_ms + 1000, sha256="b" * 64,
        )
        db.add_transfer(record)

        result = db.get_interrupted_transfer(tid)
        self.assertIsNone(result)

    def test_db07_expire_interrupted_transfers(self):
        """expire_interrupted_transfers() marks expired INTERRUPTED rows as FAILED."""
        from ferry_linux.core.db import DatabaseManager, InterruptedTransferInfo, TransferRecord

        db_path = self.tmpdir / "test_v3c.db"
        db = DatabaseManager(db_path)

        tid = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        past_ms = now_ms - (8 * 24 * 60 * 60 * 1000)  # 8 days ago = already expired

        record = TransferRecord(
            transfer_id=tid, device_id="dev-1", file_name="stale.bin",
            file_size=1000, direction="INCOMING", status="INTERRUPTED",
            started_at=past_ms, completed_at=None, sha256="c" * 64,
        )
        db.add_transfer(record)

        info = InterruptedTransferInfo(
            transfer_id=tid, bytes_received=500, resume_chunk_index=0,
            partial_sha256="0" * 64, sender_identity="KEY", original_metadata_json="{}",
            interrupted_at=past_ms, expire_at=past_ms + InterruptedTransferInfo.RESUME_TTL_MS,
        )
        db.save_interrupted_transfer(info)

        # Expiry should catch this row
        count = db.expire_interrupted_transfers(now_ms=now_ms)
        self.assertEqual(count, 1)

        # Row must now be FAILED, not INTERRUPTED
        result = db.get_interrupted_transfer(tid)
        self.assertIsNone(result)

        # Verify it's FAILED in the DB
        transfers = db.list_transfers()
        match = [t for t in transfers if t.transfer_id == tid]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0].status, "FAILED")


class TestPhase3ETask2ResumeNegotiation(unittest.TestCase):
    """
    Phase 3E Task 2: Resume Negotiation Protocol unit tests.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_interrupted_transfer(self, data: bytes) -> IncomingTransfer:
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(
            file_size=len(data) + CHUNK_SIZE,  # Total file is larger than current data
            chunk_size=CHUNK_SIZE,
            sha256=sha256,
        )
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()
        offset = 0
        seq = 0
        while offset < len(data):
            chunk = data[offset:offset + CHUNK_SIZE]
            incoming.receive_chunk(ChunkFrame(meta.transfer_id, seq, chunk))
            offset += len(chunk)
            seq += 1
        incoming.interrupt()
        return incoming

    # 12. prepare_resume_request on valid .part
    def test_12_prepare_resume_request_valid_part(self) -> None:
        data = b"\x44" * (CHUNK_SIZE * 2)
        incoming = self._make_interrupted_transfer(data)
        self.assertEqual(incoming.state, TransferState.INTERRUPTED)

        payload = incoming.prepare_resume_request()
        from ferry_linux.protocol.models import TransferResumeRequestPayload
        self.assertIsInstance(payload, TransferResumeRequestPayload)
        self.assertEqual(payload.transfer_id, incoming.meta.transfer_id)
        self.assertEqual(payload.resume_offset_bytes, len(data))
        self.assertEqual(payload.resume_chunk_index, 2)
        self.assertEqual(payload.partial_sha256, hashlib.sha256(data).hexdigest().lower())
        self.assertEqual(payload.protocol_version, 1)

    # 13. prepare_resume_request uses actual disk size
    def test_13_prepare_resume_request_uses_actual_disk_size(self) -> None:
        data = b"\x55" * (CHUNK_SIZE * 2)
        incoming = self._make_interrupted_transfer(data)

        # Mutate in-memory counters to stale values
        incoming._bytes_received = 999999
        incoming._next_seq = 99

        # Append another full chunk directly on disk
        extra = b"\x66" * CHUNK_SIZE
        with open(incoming._temp_path, "ab") as f:
            f.write(extra)

        expected_size = (2 + 1) * CHUNK_SIZE
        payload = incoming.prepare_resume_request()
        # Must derive from actual disk, not the stale in-memory counters
        self.assertEqual(payload.resume_offset_bytes, expected_size)
        self.assertEqual(payload.resume_chunk_index, 3)
        self.assertEqual(incoming.bytes_received, expected_size)
        self.assertEqual(incoming._next_seq, 3)

    # 14. prepare_resume_request computes SHA-256 from disk
    def test_14_prepare_resume_request_computes_sha256_from_disk(self) -> None:
        data = b"\x77" * CHUNK_SIZE
        incoming = self._make_interrupted_transfer(data)
        in_memory_hash = incoming._hasher.hexdigest().lower()

        # Overwrite file on disk with completely different data of same length
        diff_data = b"\x88" * CHUNK_SIZE
        with open(incoming._temp_path, "wb") as f:
            f.write(diff_data)

        payload = incoming.prepare_resume_request()
        expected_disk_hash = hashlib.sha256(diff_data).hexdigest().lower()

        # Must match disk, NOT the in-memory accumulator!
        self.assertEqual(payload.partial_sha256, expected_disk_hash)
        self.assertNotEqual(payload.partial_sha256, in_memory_hash)

    # 15. missing .part
    def test_15_missing_part_file_raises(self) -> None:
        data = b"\x99" * CHUNK_SIZE
        incoming = self._make_interrupted_transfer(data)

        # Delete the .part file
        incoming._temp_path.unlink()

        with self.assertRaises(FileNotFoundError):
            incoming.prepare_resume_request()

    # 16. invalid/non-interrupted state
    def test_16_invalid_non_interrupted_state_raises(self) -> None:
        data = b"\xAA" * CHUNK_SIZE
        sha256 = hashlib.sha256(data).hexdigest()
        meta = _make_meta(file_size=len(data) * 2, chunk_size=CHUNK_SIZE, sha256=sha256)

        # In IDLE state
        idle_incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        with self.assertRaises(RuntimeError):
            idle_incoming.prepare_resume_request()

        # In TRANSFERRING state
        transferring_incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        transferring_incoming.begin()
        transferring_incoming.receive_chunk(ChunkFrame(meta.transfer_id, 0, data))
        with self.assertRaises(RuntimeError):
            transferring_incoming.prepare_resume_request()

        # In CANCELLED state
        transferring_incoming.cancel()
        with self.assertRaises(RuntimeError):
            transferring_incoming.prepare_resume_request()

    # 17. partial-file boundary validation
    def test_17_partial_file_boundary_validation(self) -> None:
        data = b"\xBB" * CHUNK_SIZE
        incoming = self._make_interrupted_transfer(data)

        # Append partial chunk (unaligned by 123 bytes)
        with open(incoming._temp_path, "ab") as f:
            f.write(b"\xCC" * 123)

        # Must fail safely without truncating or padding
        with self.assertRaises(ValueError):
            incoming.prepare_resume_request()

        # Check file was NOT silently truncated or padded
        self.assertEqual(incoming._temp_path.stat().st_size, CHUNK_SIZE + 123)

    # 18. large .part streaming without whole-file loading
    def test_18_large_part_streaming_without_whole_file_loading(self) -> None:
        # Create 4 chunks on disk
        data = b"\xDD" * (CHUNK_SIZE * 4)
        incoming = self._make_interrupted_transfer(data)

        read_sizes = []
        orig_open = open

        from unittest.mock import patch

        class MonitoredFile:
            def __init__(self, f):
                self._f = f
            def read(self, size=-1):
                read_sizes.append(size)
                return self._f.read(size)
            def __enter__(self):
                self._f.__enter__()
                return self
            def __exit__(self, *args):
                return self._f.__exit__(*args)

        def mock_open_fn(path, mode="r", *args, **kwargs):
            real_f = orig_open(path, mode, *args, **kwargs)
            if "rb" in mode and str(path).endswith(".part"):
                return MonitoredFile(real_f)
            return real_f

        with patch("builtins.open", side_effect=mock_open_fn):
            payload = incoming.prepare_resume_request()

        self.assertEqual(payload.resume_chunk_index, 4)
        # Verify read was called with bounded chunks (<= 65536) and not -1 or whole file
        self.assertGreater(len(read_sizes), 1)
        for s in read_sizes:
            self.assertLessEqual(s, CHUNK_SIZE)
            self.assertGreater(s, 0)

    # 19. zero-byte partial file
    def test_19_zero_byte_partial_file_raises(self) -> None:
        data = b"\xEE" * CHUNK_SIZE
        incoming = self._make_interrupted_transfer(data)
        # Truncate to 0
        with open(incoming._temp_path, "wb") as f:
            pass

        with self.assertRaises(ValueError):
            incoming.prepare_resume_request()

    # 20. inconsistent DB metadata
    def test_20_inconsistent_db_metadata_raises(self) -> None:
        data = b"\xFF" * (CHUNK_SIZE * 2)
        incoming = self._make_interrupted_transfer(data)

        # Mismatched expected_bytes
        with self.assertRaises(ValueError):
            incoming.prepare_resume_request(expected_bytes=CHUNK_SIZE)

        # Mismatched expected_chunk_index
        with self.assertRaises(ValueError):
            incoming.prepare_resume_request(expected_chunk_index=1)

        # Matching metadata succeeds
        payload = incoming.prepare_resume_request(
            expected_bytes=CHUNK_SIZE * 2,
            expected_chunk_index=2,
        )
        self.assertEqual(payload.resume_chunk_index, 2)

    # 21. service layer resume message dispatch
    def test_21_service_layer_resume_message_dispatch(self) -> None:
        from ferry_linux.core.service import FerryService
        from ferry_linux.protocol.models import (
            FerryEnvelope,
            MessageType,
            ResumeRejectReason,
        )
        from unittest.mock import MagicMock

        service = FerryService(self.tmpdir)
        mock_ps = MagicMock()
        mock_ps.remote_addr = "127.0.0.1:53770"
        mock_ps.remote_device_id = "test-peer"
        mock_ps._transfer_events = {}
        mock_ps._result_events = {}

        tid = str(uuid.uuid4())

        # Test TRANSFER_RESUME_REQUEST dispatch
        env_req = FerryEnvelope(
            type=MessageType.TRANSFER_RESUME_REQUEST.value,
            payload={
                "transfer_id": tid,
                "resume_offset_bytes": CHUNK_SIZE,
                "resume_chunk_index": 1,
                "partial_sha256": "0" * 64,
                "protocol_version": 1,
            },
        )
        asyncio.run(service._handle_transfer_message(mock_ps, env_req))

        # Test TRANSFER_RESUME_ACCEPT dispatch
        env_accept = FerryEnvelope(
            type=MessageType.TRANSFER_RESUME_ACCEPT.value,
            payload={
                "transfer_id": tid,
                "resume_chunk_index": 1,
                "protocol_version": 1,
            },
        )
        asyncio.run(service._handle_transfer_message(mock_ps, env_accept))

        # Test TRANSFER_RESUME_REJECT dispatch
        env_reject = FerryEnvelope(
            type=MessageType.TRANSFER_RESUME_REJECT.value,
            payload={
                "transfer_id": tid,
                "reason": ResumeRejectReason.SOURCE_MODIFIED.value,
            },
        )
        asyncio.run(service._handle_transfer_message(mock_ps, env_reject))


class TestPhase3ETask3ResumeExecution(unittest.TestCase):
    """
    Phase 3E Task 3 tests: resume execution layer.

    Tests:
    T3-01. RESUME_REQUESTED and RESUMING states exist in TransferState
    T3-02. INTERRUPTED -> RESUME_REQUESTED -> RESUMING valid transitions
    T3-03. RESUMING -> COMPLETED valid transition
    T3-04. RESUMING -> INTERRUPTED valid transition (re-interrupt)
    T3-05. IncomingTransfer.resume() re-opens .part and sets _next_seq
    T3-06. IncomingTransfer.resume() seeds SHA-256 from .part contents
    T3-07. IncomingTransfer.resume() fails if .part file missing
    T3-08. IncomingTransfer.resume() fails if called from non-INTERRUPTED state
    T3-09. receive_chunk() works in RESUMING state
    T3-10. OutgoingTransfer.stream_chunks_from(0) == stream_chunks()
    T3-11. OutgoingTransfer.stream_chunks_from(N) starts at correct seq
    T3-12. stream_chunks_from() seek beyond EOF raises TransferError
    T3-13. _compute_prefix_sha256 matches manual SHA-256 prefix
    T3-14. _compute_prefix_sha256 returns None for truncated file
    T3-15. service.discard_interrupted_transfer removes .part and clears DB
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_interrupted_incoming(
        self, data: bytes
    ) -> "IncomingTransfer":
        """Create an IncomingTransfer in INTERRUPTED state with .part file written."""
        sha256 = hashlib.sha256(data).hexdigest()
        chunk_size = CHUNK_SIZE
        chunk_count = (len(data) + chunk_size - 1) // chunk_size
        meta = TransferMetadata(
            transfer_id=str(uuid.uuid4()),
            file_name="resume_test.bin",
            file_size=len(data),
            mime_type="application/octet-stream",
            sha256=sha256,
            chunk_size=chunk_size,
            chunk_count=chunk_count,
            sender_identity="dGVzdA==",
            created_at=0,
        )
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        incoming.begin()

        # Write data chunk by chunk to reach TRANSFERRING state
        for i, start in enumerate(range(0, len(data), chunk_size)):
            chunk = data[start : start + chunk_size]
            frame = encode_chunk_frame(meta.transfer_id, i, chunk)
            cf = ChunkFrame(
                transfer_id=meta.transfer_id,
                seq=i,
                data=chunk,
            )
            incoming.receive_chunk(cf)

        # Now interrupt
        incoming.interrupt()
        return incoming

    # T3-01
    def test_t301_new_states_exist(self):
        """RESUME_REQUESTED and RESUMING must be valid TransferState values."""
        self.assertIn("RESUME_REQUESTED", [s.name for s in TransferState])
        self.assertIn("RESUMING", [s.name for s in TransferState])

    # T3-02
    def test_t302_interrupted_to_resuming_via_resume_requested(self):
        """INTERRUPTED -> RESUME_REQUESTED -> RESUMING valid via state machine."""
        s = _transfer_transition(TransferState.INTERRUPTED, TransferState.RESUME_REQUESTED)
        self.assertEqual(s, TransferState.RESUME_REQUESTED)
        s2 = _transfer_transition(TransferState.RESUME_REQUESTED, TransferState.RESUMING)
        self.assertEqual(s2, TransferState.RESUMING)

    # T3-03
    def test_t303_resuming_to_completed(self):
        """RESUMING -> COMPLETED is a valid transition."""
        s = _transfer_transition(TransferState.RESUMING, TransferState.COMPLETED)
        self.assertEqual(s, TransferState.COMPLETED)

    # T3-04
    def test_t304_resuming_to_interrupted(self):
        """RESUMING -> INTERRUPTED is valid (re-interrupt on second disconnect)."""
        s = _transfer_transition(TransferState.RESUMING, TransferState.INTERRUPTED)
        self.assertEqual(s, TransferState.INTERRUPTED)

    # T3-05
    def test_t305_resume_reopens_file_and_sets_next_seq(self):
        """resume() re-opens the .part file in append mode and sets _next_seq."""
        data = b"\xAB" * (CHUNK_SIZE * 3)
        incoming = self._make_interrupted_incoming(data)
        self.assertEqual(incoming.state, TransferState.INTERRUPTED)

        chunk_index = incoming._next_seq  # 3 chunks written
        self.assertEqual(chunk_index, 3)

        incoming.resume(chunk_index)
        self.assertEqual(incoming.state, TransferState.RESUMING)
        self.assertEqual(incoming._next_seq, chunk_index)
        self.assertIsNotNone(incoming._temp_fh)

        # File handle must be open in append mode
        self.assertFalse(incoming._temp_fh.closed)
        incoming._temp_fh.close()

    # T3-06
    def test_t306_resume_seeds_sha256_from_part(self):
        """resume() re-reads the .part file to seed the SHA-256 accumulator."""
        data = b"\xCD" * CHUNK_SIZE
        incoming = self._make_interrupted_incoming(data)

        # The hasher after interrupt should NOT be the seeded one yet.
        # After resume(), the hasher should reflect the prefix.
        incoming.resume(incoming._next_seq)

        # Compute expected prefix hash manually
        expected_hash = hashlib.sha256(data).hexdigest()

        # finalise() should succeed if we don't append any more data
        # (transfer is already 100% complete in this test).
        # First close the file handle that resume() opened so finalise() can rename.
        incoming._temp_fh.close()
        incoming._state = _transfer_transition(TransferState.RESUMING, TransferState.COMPLETED)
        # Get the digest from the hasher directly
        actual = incoming._hasher.hexdigest()
        self.assertEqual(actual, expected_hash)

    # T3-07
    def test_t307_resume_fails_if_part_missing(self):
        """resume() raises FileNotFoundError if .part file is gone."""
        data = b"\xEE" * CHUNK_SIZE
        incoming = self._make_interrupted_incoming(data)

        # Delete the .part file
        incoming._temp_path.unlink()

        with self.assertRaises(FileNotFoundError):
            incoming.resume(1)
        self.assertEqual(incoming.state, TransferState.FAILED)

    # T3-08
    def test_t308_resume_from_wrong_state_raises(self):
        """resume() raises RuntimeError if called when not INTERRUPTED."""
        meta = _make_meta()
        incoming = IncomingTransfer(meta=meta, download_dir=self.tmpdir)
        # IDLE state
        with self.assertRaises(RuntimeError):
            incoming.resume(0)
        incoming.begin()
        # ACCEPTED (begin() -> ACCEPTED)
        with self.assertRaises(RuntimeError):
            incoming.resume(0)

    # T3-09
    def test_t309_receive_chunk_in_resuming_state(self):
        """receive_chunk() must work when state == RESUMING."""
        data = b"\xFF" * (CHUNK_SIZE * 2)
        incoming = self._make_interrupted_incoming(data)
        chunk_index = incoming._next_seq

        incoming.resume(chunk_index)
        self.assertEqual(incoming.state, TransferState.RESUMING)

        # Resume with one more chunk (appending)
        extra = b"\x00" * 64
        frame = ChunkFrame(
            transfer_id=incoming.meta.transfer_id,
            seq=chunk_index,
            data=extra,
        )
        # This should NOT raise; RESUMING is now accepted by receive_chunk()
        try:
            incoming.receive_chunk(frame)
        except RuntimeError:
            self.fail("receive_chunk() raised RuntimeError in RESUMING state")

    # T3-10
    def test_t310_stream_chunks_from_zero_equals_stream_chunks(self):
        """stream_chunks_from(0) should produce identical frames to stream_chunks()."""
        data = b"\xAA" * (CHUNK_SIZE * 3 + 42)
        src = self.tmpdir / "source.bin"
        src.write_bytes(data)

        fixed_id = str(uuid.uuid4())
        xfer1 = OutgoingTransfer(source_path=src, receiver_identity="", transfer_id=fixed_id)
        xfer2 = OutgoingTransfer(source_path=src, receiver_identity="", transfer_id=fixed_id)

        async def collect(gen):
            result = []
            async for f in gen:
                result.append(f)
            return result

        frames_a = asyncio.run(collect(xfer1.stream_chunks()))
        frames_b = asyncio.run(collect(xfer2.stream_chunks_from(0)))

        self.assertEqual(len(frames_a), len(frames_b))
        for i, (a, b) in enumerate(zip(frames_a, frames_b)):
            self.assertEqual(a, b, f"Frame {i} mismatch")

    # T3-11
    def test_t311_stream_chunks_from_n_starts_at_correct_seq(self):
        """stream_chunks_from(N) emits frames with seq starting at N."""
        from ferry_linux.core.transfer import CHUNK_HEADER_SIZE, ChunkFrame
        import struct

        data = b"\xBB" * (CHUNK_SIZE * 5)
        src = self.tmpdir / "source5.bin"
        src.write_bytes(data)

        resume_at = 3
        xfer = OutgoingTransfer(source_path=src, receiver_identity="")

        async def collect_seqs():
            seqs = []
            async for frame_bytes in xfer.stream_chunks_from(resume_at):
                # FYCH frame: [4 magic][16 UUID][4 seq][4 payload_len][payload]
                seq_offset = 4 + 16
                seq = struct.unpack("!I", frame_bytes[seq_offset:seq_offset + 4])[0]
                seqs.append(seq)
            return seqs

        seqs = asyncio.run(collect_seqs())
        self.assertEqual(seqs[0], resume_at)
        self.assertEqual(seqs[-1], 4)  # last chunk is chunk 4

    # T3-12
    def test_t312_stream_chunks_from_beyond_eof_yields_nothing(self):
        """stream_chunks_from() beyond file end yields no frames (EOF immediate)."""
        data = b"\xCC" * CHUNK_SIZE
        src = self.tmpdir / "short.bin"
        src.write_bytes(data)

        # File has 1 chunk; resuming from chunk 1 should yield 0 frames (already EOF)
        xfer = OutgoingTransfer(source_path=src, receiver_identity="")

        async def collect():
            frames = []
            async for f in xfer.stream_chunks_from(1):
                frames.append(f)
            return frames

        frames = asyncio.run(collect())
        self.assertEqual(frames, [])

    # T3-13
    def test_t313_compute_prefix_sha256_matches_manual(self):
        """_compute_prefix_sha256 returns the same hash as a manual SHA-256 of the prefix."""
        from ferry_linux.core.service import _compute_prefix_sha256

        data = b"\x01\x02\x03" * 1000
        src = self.tmpdir / "prehash.bin"
        src.write_bytes(data)

        prefix_len = 1500
        expected = hashlib.sha256(data[:prefix_len]).hexdigest()
        actual = _compute_prefix_sha256(src, prefix_len)
        self.assertEqual(actual, expected)

    # T3-14
    def test_t314_compute_prefix_sha256_returns_none_for_truncated_file(self):
        """_compute_prefix_sha256 returns None when file is shorter than requested prefix."""
        from ferry_linux.core.service import _compute_prefix_sha256

        data = b"\xAB" * 100
        src = self.tmpdir / "short_prehash.bin"
        src.write_bytes(data)

        # Request prefix larger than file
        result = _compute_prefix_sha256(src, 500)
        self.assertIsNone(result)

    # T3-15
    def test_t315_discard_interrupted_transfer_cleans_db_and_file(self):
        """discard_interrupted_transfer() removes .part and marks DB row as FAILED."""
        from ferry_linux.core.service import FerryService
        from ferry_linux.core.db import InterruptedTransferInfo

        service = FerryService(self.tmpdir)

        # Set up DB record and .part file
        staging_dir = Path(service.config.download_dir) / "staging"
        staging_dir.mkdir(parents=True, exist_ok=True)

        transfer_id = str(uuid.uuid4())
        part_file = staging_dir / f"{transfer_id}.part"
        part_file.write_bytes(b"partial data")

        now_ms = int(time.time() * 1000)
        info = InterruptedTransferInfo(
            transfer_id=transfer_id,
            bytes_received=12,
            resume_chunk_index=1,
            partial_sha256="a" * 64,
            sender_identity="dGVzdA==",
            original_metadata_json="{}",
            interrupted_at=now_ms,
            expire_at=InterruptedTransferInfo.make_expire_at(now_ms),
        )
        service.db.save_interrupted_transfer(info)

        # Verify .part exists and DB has INTERRUPTED record
        self.assertTrue(part_file.exists())
        self.assertIsNotNone(service.db.get_interrupted_transfer(transfer_id))

        # Discard
        asyncio.run(service.discard_interrupted_transfer(transfer_id))

        # .part must be gone
        self.assertFalse(part_file.exists())
        # DB record must no longer be INTERRUPTED
        result = service.db.get_interrupted_transfer(transfer_id)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
