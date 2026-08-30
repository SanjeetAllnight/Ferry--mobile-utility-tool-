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


if __name__ == "__main__":
    unittest.main()
