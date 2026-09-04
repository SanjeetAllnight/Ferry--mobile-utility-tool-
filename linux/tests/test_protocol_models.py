"""
Unit tests for Ferry Protocol Models and Binary Framing.
"""

import unittest
import uuid

from ferry_linux.protocol.models import (
    MAGIC_BYTES,
    PROTOCOL_VERSION,
    FerryEnvelope,
    MessageType,
    TransferRequestPayload,
    TransferCompletePayload,
    TransferResultPayload,
    ResumeRejectReason,
    TransferResumeRequestPayload,
    TransferResumeAcceptPayload,
    TransferResumeRejectPayload,
    decode_frame,
    encode_frame,
)

CHUNK_SIZE = 65536


class TestProtocolModels(unittest.TestCase):

    def test_envelope_serialization(self) -> None:
        payload = {"device_name": "Test Peer", "public_key": "abc"}
        env = FerryEnvelope(type=MessageType.HANDSHAKE_INIT.value, payload=payload)

        json_str = env.to_json()
        restored = FerryEnvelope.from_json(json_str)

        self.assertEqual(restored.protocol_version, PROTOCOL_VERSION)
        self.assertEqual(restored.type, MessageType.HANDSHAKE_INIT.value)
        self.assertEqual(restored.payload["device_name"], "Test Peer")
        self.assertEqual(restored.message_id, env.message_id)

    def test_transfer_request_payload(self) -> None:
        tid = str(uuid.uuid4())
        req = TransferRequestPayload(
            transfer_id=tid,
            file_name="sample.pdf",
            file_size=5000,
            mime_type="application/pdf",
            sha256="a" * 64,
            chunk_size=CHUNK_SIZE,
            chunk_count=1,
            sender_identity="key",
            created_at=0,
        )

        data = req.to_dict()
        restored = TransferRequestPayload.from_dict(data)

        self.assertEqual(restored.transfer_id, tid)
        self.assertEqual(restored.file_name, "sample.pdf")
        self.assertEqual(restored.file_size, 5000)
        self.assertEqual(restored.chunk_count, 1)

    def test_binary_frame_encode_decode(self) -> None:
        env = FerryEnvelope(
            type=MessageType.PAIR_REQUEST.value,
            payload={"sas_code": "123456", "timeout": 30},
        )
        framed_bytes = encode_frame(env)

        # Check framing prefix
        self.assertEqual(framed_bytes[:2], MAGIC_BYTES)
        self.assertGreater(len(framed_bytes), 6)

        # Decode valid frame
        decoded_env, consumed = decode_frame(framed_bytes)
        self.assertIsNotNone(decoded_env)
        self.assertEqual(consumed, len(framed_bytes))
        self.assertEqual(decoded_env.type, MessageType.PAIR_REQUEST.value)
        self.assertEqual(decoded_env.payload["sas_code"], "123456")

    def test_decode_incomplete_frame(self) -> None:
        env = FerryEnvelope(type=MessageType.TRANSFER_CANCEL.value, payload={})
        framed_bytes = encode_frame(env)

        # Partial header (< 6 bytes)
        partial, consumed = decode_frame(framed_bytes[:4])
        self.assertIsNone(partial)
        self.assertEqual(consumed, 0)

        # Partial payload
        partial_payload, consumed = decode_frame(framed_bytes[:10])
        self.assertIsNone(partial_payload)
        self.assertEqual(consumed, 0)

    def test_decode_corrupted_magic(self) -> None:
        corrupted = b"XX\x00\x00\x00\x05{}"
        with self.assertRaises(ValueError):
            decode_frame(corrupted)


class TestResumeProtocolModels(unittest.TestCase):
    """
    Phase 3E Task 2 protocol validation and serialization unit tests.
    """

    def setUp(self) -> None:
        self.valid_uuid = str(uuid.uuid4())
        self.valid_sha256 = "0123456789abcdef" * 4  # 64-char lowercase hex
        self.chunk_size = 65536
        self.valid_chunk_idx = 112
        self.valid_offset = self.valid_chunk_idx * self.chunk_size  # 7340032

    # 1. TRANSFER_RESUME_REQUEST serialization
    def test_01_transfer_resume_request_serialization(self) -> None:
        req = TransferResumeRequestPayload(
            transfer_id=self.valid_uuid,
            resume_offset_bytes=self.valid_offset,
            resume_chunk_index=self.valid_chunk_idx,
            partial_sha256=self.valid_sha256,
            protocol_version=1,
        )
        data = req.to_dict()
        self.assertEqual(data["transfer_id"], self.valid_uuid)
        self.assertEqual(data["resume_offset_bytes"], self.valid_offset)
        self.assertEqual(data["resume_chunk_index"], self.valid_chunk_idx)
        self.assertEqual(data["partial_sha256"], self.valid_sha256)
        self.assertEqual(data["protocol_version"], 1)

        # FY framing
        env = FerryEnvelope(type=MessageType.TRANSFER_RESUME_REQUEST.value, payload=data)
        framed = encode_frame(env)
        self.assertTrue(framed.startswith(MAGIC_BYTES))
        decoded_env, consumed = decode_frame(framed)
        self.assertIsNotNone(decoded_env)
        self.assertEqual(decoded_env.type, MessageType.TRANSFER_RESUME_REQUEST.value)
        self.assertEqual(decoded_env.payload["transfer_id"], self.valid_uuid)

    # 2. TRANSFER_RESUME_REQUEST deserialization
    def test_02_transfer_resume_request_deserialization(self) -> None:
        data = {
            "transfer_id": self.valid_uuid,
            "resume_offset_bytes": self.valid_offset,
            "resume_chunk_index": self.valid_chunk_idx,
            "partial_sha256": self.valid_sha256,
            "protocol_version": 1,
        }
        req = TransferResumeRequestPayload.from_dict(data)
        self.assertEqual(req.transfer_id, self.valid_uuid)
        self.assertEqual(req.resume_offset_bytes, self.valid_offset)
        self.assertEqual(req.resume_chunk_index, self.valid_chunk_idx)
        self.assertEqual(req.partial_sha256, self.valid_sha256)
        self.assertEqual(req.protocol_version, 1)
        # Dict-like access
        self.assertEqual(req["transfer_id"], self.valid_uuid)

    # 3. invalid transfer ID
    def test_03_invalid_transfer_id(self) -> None:
        bad_ids = ["", "not-a-uuid", "12345", "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz"]
        for bad_id in bad_ids:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    TransferResumeRequestPayload(
                        transfer_id=bad_id,
                        resume_offset_bytes=self.valid_offset,
                        resume_chunk_index=self.valid_chunk_idx,
                        partial_sha256=self.valid_sha256,
                        protocol_version=1,
                    )

    # 4. negative offset
    def test_04_negative_offset(self) -> None:
        with self.assertRaises(ValueError):
            TransferResumeRequestPayload(
                transfer_id=self.valid_uuid,
                resume_offset_bytes=-1,
                resume_chunk_index=0,
                partial_sha256=self.valid_sha256,
                protocol_version=1,
            )

    # 5. negative chunk index
    def test_05_negative_chunk_index(self) -> None:
        with self.assertRaises(ValueError):
            TransferResumeRequestPayload(
                transfer_id=self.valid_uuid,
                resume_offset_bytes=0,
                resume_chunk_index=-1,
                partial_sha256=self.valid_sha256,
                protocol_version=1,
            )

    # 6. invalid SHA-256
    def test_06_invalid_sha256(self) -> None:
        bad_hashes = [
            "abc",  # too short
            "a" * 63,  # 63 chars
            "a" * 65,  # 65 chars
            "g" * 64,  # non-hex
            self.valid_sha256.upper(),  # uppercase hex (must be lowercase)
            " " * 64,  # whitespace
        ]
        for bad_hash in bad_hashes:
            with self.subTest(bad_hash=bad_hash):
                with self.assertRaises(ValueError):
                    TransferResumeRequestPayload(
                        transfer_id=self.valid_uuid,
                        resume_offset_bytes=self.valid_offset,
                        resume_chunk_index=self.valid_chunk_idx,
                        partial_sha256=bad_hash,
                        protocol_version=1,
                    )

    # 7. invalid protocol version
    def test_07_invalid_protocol_version(self) -> None:
        bad_versions = [0, 2, -1, 99]
        for bad_v in bad_versions:
            with self.subTest(bad_v=bad_v):
                with self.assertRaises(ValueError):
                    TransferResumeRequestPayload(
                        transfer_id=self.valid_uuid,
                        resume_offset_bytes=self.valid_offset,
                        resume_chunk_index=self.valid_chunk_idx,
                        partial_sha256=self.valid_sha256,
                        protocol_version=bad_v,
                    )

    # 8. offset/chunk mismatch
    def test_08_offset_chunk_mismatch(self) -> None:
        # chunk index is 0 but offset > 0
        with self.assertRaises(ValueError):
            TransferResumeRequestPayload(
                transfer_id=self.valid_uuid,
                resume_offset_bytes=65536,
                resume_chunk_index=0,
                partial_sha256=self.valid_sha256,
                protocol_version=1,
            )
        # chunk index is 2 but offset is 1 chunk (65536 != 2 * 65536)
        with self.assertRaises(ValueError):
            TransferResumeRequestPayload(
                transfer_id=self.valid_uuid,
                resume_offset_bytes=65536,
                resume_chunk_index=2,
                partial_sha256=self.valid_sha256,
                protocol_version=1,
            )
        # arbitrary mismatch
        with self.assertRaises(ValueError):
            TransferResumeRequestPayload(
                transfer_id=self.valid_uuid,
                resume_offset_bytes=7340032,
                resume_chunk_index=100,
                partial_sha256=self.valid_sha256,
                protocol_version=1,
            )

    # 9. TRANSFER_RESUME_ACCEPT serialization
    def test_09_transfer_resume_accept_serialization_and_validation(self) -> None:
        accept = TransferResumeAcceptPayload(
            transfer_id=self.valid_uuid,
            resume_chunk_index=self.valid_chunk_idx,
            protocol_version=1,
        )
        data = accept.to_dict()
        self.assertEqual(data["transfer_id"], self.valid_uuid)
        self.assertEqual(data["resume_chunk_index"], self.valid_chunk_idx)
        self.assertEqual(data["protocol_version"], 1)

        restored = TransferResumeAcceptPayload.from_dict(data)
        self.assertEqual(restored.transfer_id, self.valid_uuid)
        self.assertEqual(restored.resume_chunk_index, self.valid_chunk_idx)

        # Invalid UUID
        with self.assertRaises(ValueError):
            TransferResumeAcceptPayload(
                transfer_id="bad-uuid",
                resume_chunk_index=self.valid_chunk_idx,
            )
        # Negative chunk index
        with self.assertRaises(ValueError):
            TransferResumeAcceptPayload(
                transfer_id=self.valid_uuid,
                resume_chunk_index=-1,
            )
        # Invalid version
        with self.assertRaises(ValueError):
            TransferResumeAcceptPayload(
                transfer_id=self.valid_uuid,
                resume_chunk_index=0,
                protocol_version=2,
            )

    # 10. TRANSFER_RESUME_REJECT serialization
    def test_10_transfer_resume_reject_serialization(self) -> None:
        reject = TransferResumeRejectPayload(
            transfer_id=self.valid_uuid,
            reason=ResumeRejectReason.SOURCE_MODIFIED.value,
        )
        data = reject.to_dict()
        self.assertEqual(data["transfer_id"], self.valid_uuid)
        self.assertEqual(data["reason"], "SOURCE_MODIFIED")

        restored = TransferResumeRejectPayload.from_dict(data)
        self.assertEqual(restored.transfer_id, self.valid_uuid)
        self.assertEqual(restored.reason, "SOURCE_MODIFIED")

    # 11. invalid reject reason
    def test_11_invalid_reject_reason(self) -> None:
        valid_reasons = [
            ResumeRejectReason.SOURCE_MODIFIED,
            ResumeRejectReason.TRANSFER_NOT_FOUND,
            ResumeRejectReason.PARTIAL_CORRUPT,
            ResumeRejectReason.WRONG_PEER,
            ResumeRejectReason.STALE,
            ResumeRejectReason.PEER_CANCELLED,
        ]
        # All 6 must be valid
        for reason in valid_reasons:
            p = TransferResumeRejectPayload(transfer_id=self.valid_uuid, reason=reason.value)
            self.assertEqual(p.reason, reason.value)

        # Unknown reasons must be rejected
        bad_reasons = ["RANDOM_REASON", "INVALID", "", "USER_REJECTED", "BUSY"]
        for bad_reason in bad_reasons:
            with self.subTest(bad_reason=bad_reason):
                with self.assertRaises(ValueError):
                    TransferResumeRejectPayload(
                        transfer_id=self.valid_uuid,
                        reason=bad_reason,
                    )


if __name__ == "__main__":
    unittest.main()
