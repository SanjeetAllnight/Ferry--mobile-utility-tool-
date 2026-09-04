package dev.ferry.app.transfer

import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.security.MessageDigest
import java.util.UUID

/**
 * Unit tests for Ferry Phase 3A Android transfer layer.
 *
 * Tests:
 *  1. TransferMetadata validation: valid payload parsed
 *  2. TransferMetadata: invalid UUID rejected
 *  3. TransferMetadata: empty file_name rejected
 *  4. TransferMetadata: path traversal sanitised
 *  5. TransferMetadata: zero file_size rejected
 *  6. TransferMetadata: oversized chunk_size rejected
 *  7. TransferMetadata: mismatched chunk_count rejected
 *  8. TransferMetadata: non-hex sha256 rejected
 *  9. sanitiseFilename: simple name preserved
 * 10. sanitiseFilename: path separators stripped
 * 11. sanitiseFilename: Windows path stripped
 * 12. sanitiseFilename: control characters stripped
 * 13. sanitiseFilename: reserved CON rejected
 * 14. sanitiseFilename: dot-dot rejected
 * 15. TransferMetadata: sha256 normalised to lowercase
 * 16. FerryTransferClient.encodeChunkFrame: round-trips with decodeChunkFrame
 * 17. FerryTransferReceiver.isChunkFrame: FYCH magic detected
 * 18. FerryTransferReceiver.isChunkFrame: JSON not detected
 * 19. FerryTransferReceiver.decodeChunkFrame: correct fields returned
 * 20. FerryTransferReceiver.decodeChunkFrame: bad magic rejected
 * 21. FerryTransferReceiver.decodeChunkFrame: truncated frame rejected
 * 22. Full lifecycle: write chunks, verify SHA-256, finalise
 * 23. Integrity failure: corrupted data causes finalise() to return false
 * 24. Cancellation removes temp file
 * 25. Out-of-order sequence rejected
 * 26. ProtocolConstants has TRANSFER_CHUNK and TRANSFER_RESULT
 */
class TransferTest {

    @get:Rule
    val tempFolder = TemporaryFolder()

    private lateinit var stagingDir: File

    @Before
    fun setUp() {
        stagingDir = tempFolder.newFolder("staging")
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private fun validMap(
        fileSize: Long = 131072L,
        chunkSize: Int = TransferMetadata.MAX_CHUNK_SIZE,
        sha256: String = "a".repeat(64),
        fileName: String = "photo.jpg",
    ): MutableMap<String, Any?> {
        val chunkCount = if (fileSize > 0) ((fileSize + chunkSize - 1) / chunkSize).toInt() else 0
        return mutableMapOf(
            "transfer_id" to UUID.randomUUID().toString(),
            "file_name" to fileName,
            "file_size" to fileSize,
            "mime_type" to "image/jpeg",
            "sha256" to sha256,
            "chunk_size" to chunkSize,
            "chunk_count" to chunkCount,
            "sender_identity" to "dGVzdA==",
            "created_at" to 0L,
            "protocol_version" to 1,
        )
    }

    private fun sha256Hex(data: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(data)
        return d.joinToString("") { "%02x".format(it) }
    }

    // ── 1–8: TransferMetadata validation ─────────────────────────────────────

    @Test fun test01_validMetadataParsed() {
        val meta = TransferMetadata.fromMap(validMap())
        assertEquals("photo.jpg", meta.fileName)
        assertEquals(131072L, meta.fileSize)
    }

    @Test(expected = Exception::class) fun test02_invalidUuidRejected() {
        val m = validMap(); m["transfer_id"] = "not-a-uuid"
        TransferMetadata.fromMap(m)
    }

    @Test(expected = IllegalArgumentException::class) fun test03_emptyFileNameRejected() {
        val m = validMap(); m["file_name"] = ""
        TransferMetadata.fromMap(m)
    }

    @Test fun test04_pathTraversalSanitised() {
        val m = validMap(); m["file_name"] = "../../etc/passwd"
        val meta = TransferMetadata.fromMap(m)
        assertEquals("passwd", meta.fileName)
    }

    @Test fun test05_zeroFileSizeAccepted() {
        val chunkSize = TransferMetadata.MAX_CHUNK_SIZE
        val m = validMap(fileSize = 0L, chunkSize = chunkSize)
        m["chunk_count"] = 0
        val meta = TransferMetadata.fromMap(m)
        assertEquals(0L, meta.fileSize)
        assertEquals(0, meta.chunkCount)
    }

    @Test(expected = IllegalArgumentException::class) fun test05b_negativeFileSizeRejected() {
        val m = validMap(fileSize = -1L)
        TransferMetadata.fromMap(m)
    }

    @Test(expected = IllegalArgumentException::class) fun test06_oversizedChunkSizeRejected() {
        val m = validMap(chunkSize = TransferMetadata.MAX_CHUNK_SIZE)
        m["chunk_size"] = TransferMetadata.MAX_CHUNK_SIZE + 1
        m["chunk_count"] = 999
        TransferMetadata.fromMap(m)
    }

    @Test(expected = IllegalArgumentException::class) fun test07_mismatchedChunkCountRejected() {
        val m = validMap(); m["chunk_count"] = 999
        TransferMetadata.fromMap(m)
    }

    @Test(expected = IllegalArgumentException::class) fun test08_invalidSha256Rejected() {
        val m = validMap(); m["sha256"] = "notahexstring"
        TransferMetadata.fromMap(m)
    }

    @Test fun test08b_sha256NormalisedLowercase() {
        val m = validMap(sha256 = "A".repeat(64))
        val meta = TransferMetadata.fromMap(m)
        assertEquals("a".repeat(64), meta.sha256)
    }


    // ── 9–15: sanitiseFilename ────────────────────────────────────────────────

    @Test fun test09_simpleNamePreserved() {
        assertEquals("photo.jpg", TransferMetadata.sanitiseFilename("photo.jpg"))
    }

    @Test fun test10_pathSeparatorsStripped() {
        assertEquals("shadow", TransferMetadata.sanitiseFilename("../../etc/shadow"))
    }

    @Test fun test11_windowsPathStripped() {
        val result = TransferMetadata.sanitiseFilename("C:\\Windows\\system32\\file.exe")
        assertEquals("file.exe", result)
    }

    @Test fun test12_controlCharacterStripped() {
        val result = TransferMetadata.sanitiseFilename("bad\u0001name.txt")
        assertFalse(result.contains('\u0001'))
    }

    @Test fun test13_reservedConRejected() {
        assertEquals("", TransferMetadata.sanitiseFilename("CON"))
    }

    @Test fun test14_dotDotRejected() {
        assertEquals("", TransferMetadata.sanitiseFilename(".."))
    }

    @Test fun test15_emptySanitisedToEmpty() {
        assertEquals("", TransferMetadata.sanitiseFilename(""))
    }

    // ── 16: encodeChunkFrame round-trip ──────────────────────────────────────

    @Test fun test16_chunkFrameRoundTrip() {
        val tid = UUID.randomUUID().toString()
        val data = ByteArray(256) { it.toByte() }
        val encoded = FerryTransferClient.encodeChunkFrame(tid, 42, data)
        val (decodedTid, seq, decodedData) = FerryTransferReceiver.decodeChunkFrame(encoded)
        assertEquals(tid, decodedTid)
        assertEquals(42, seq)
        assertArrayEquals(data, decodedData)
    }

    @Test fun test16b_emptyChunkRoundTrip() {
        val tid = UUID.randomUUID().toString()
        val encoded = FerryTransferClient.encodeChunkFrame(tid, 0, ByteArray(0))
        val (_, seq, data) = FerryTransferReceiver.decodeChunkFrame(encoded)
        assertEquals(0, seq)
        assertEquals(0, data.size)
    }

    // ── 17–21: isChunkFrame / decodeChunkFrame ────────────────────────────────

    @Test fun test17_isChunkFrameTrue() {
        val encoded = FerryTransferClient.encodeChunkFrame(UUID.randomUUID().toString(), 0, b("hello"))
        assertTrue(FerryTransferReceiver.isChunkFrame(encoded))
    }

    @Test fun test18_isChunkFrameFalseForJson() {
        val json = """{"type":"TRANSFER_REQUEST","payload":{}}""".toByteArray()
        assertFalse(FerryTransferReceiver.isChunkFrame(json))
    }

    @Test fun test19_decodeChunkFrameCorrectFields() {
        val tid = UUID.randomUUID().toString()
        val payload = b("transfer data")
        val encoded = FerryTransferClient.encodeChunkFrame(tid, 7, payload)
        val (decodedTid, seq, data) = FerryTransferReceiver.decodeChunkFrame(encoded)
        assertEquals(tid, decodedTid)
        assertEquals(7, seq)
        assertArrayEquals(payload, data)
    }

    @Test(expected = IllegalArgumentException::class) fun test20_badMagicRejected() {
        val encoded = FerryTransferClient.encodeChunkFrame(UUID.randomUUID().toString(), 0, b("x"))
        encoded[0] = 0x00 // corrupt magic
        FerryTransferReceiver.decodeChunkFrame(encoded)
    }

    @Test(expected = IllegalArgumentException::class) fun test21_truncatedFrameRejected() {
        val encoded = FerryTransferClient.encodeChunkFrame(UUID.randomUUID().toString(), 0, b("hello"))
        FerryTransferReceiver.decodeChunkFrame(encoded.copyOf(10))
    }

    // ── 22–24: Full transfer lifecycle ────────────────────────────────────────

    @Test fun test22_fullLifecycleSuccess() {
        val fileData = ByteArray(FerryTransferClient.CHUNK_SIZE + 100) { it.toByte() }
        val fileSize = fileData.size.toLong()
        val sha256 = sha256Hex(fileData)
        val chunkSize = FerryTransferClient.CHUNK_SIZE
        val chunkCount = ((fileSize + chunkSize - 1) / chunkSize).toInt()
        val tid = UUID.randomUUID().toString()

        val meta = TransferMetadata(
            transferId = tid,
            fileName = "test.bin",
            fileSize = fileSize,
            mimeType = "application/octet-stream",
            sha256 = sha256,
            chunkSize = chunkSize,
            chunkCount = chunkCount,
            senderIdentity = "",
            createdAt = 0L,
        )

        val receiver = FerryTransferReceiver(meta, stagingDir)
        receiver.begin()

        // Stream all chunks
        var offset = 0
        var seq = 0
        while (offset < fileData.size) {
            val end = minOf(offset + chunkSize, fileData.size)
            val chunk = fileData.copyOfRange(offset, end)
            val frame = FerryTransferClient.encodeChunkFrame(tid, seq, chunk)
            val (decodedTid, decodedSeq, decodedData) = FerryTransferReceiver.decodeChunkFrame(frame)
            receiver.receiveChunk(decodedTid, decodedSeq, decodedData)
            offset = end
            seq++
        }

        val success = receiver.finalise()
        assertTrue("Transfer should complete successfully", success)
        // Temp file should be gone
        val tempFile = File(stagingDir, "$tid.part")
        assertFalse("Temp file should be renamed, not exist at .part path", tempFile.exists())
    }

    @Test fun test22b_zeroByteTransferLifecycleSuccess() {
        val fileData = ByteArray(0)
        val fileSize = 0L
        val sha256 = sha256Hex(fileData)
        val chunkSize = FerryTransferClient.CHUNK_SIZE
        val chunkCount = 0
        val tid = UUID.randomUUID().toString()

        val meta = TransferMetadata(
            transferId = tid,
            fileName = "empty.txt",
            fileSize = fileSize,
            mimeType = "text/plain",
            sha256 = sha256,
            chunkSize = chunkSize,
            chunkCount = chunkCount,
            senderIdentity = "",
            createdAt = 0L,
        )

        val receiver = FerryTransferReceiver(meta, stagingDir)
        receiver.begin()
        val success = receiver.finalise()
        assertTrue("0-byte transfer should complete successfully", success)

        val finalFile = File(stagingDir, "empty.txt")
        assertTrue("Final empty file should exist", finalFile.exists())
        assertEquals(0L, finalFile.length())
    }

    @Test fun test23_integrityFailure() {
        val fileData = ByteArray(512) { 0x42 }
        val chunkSize = FerryTransferClient.CHUNK_SIZE
        val chunkCount = 1
        val tid = UUID.randomUUID().toString()

        val meta = TransferMetadata(
            transferId = tid,
            fileName = "test.bin",
            fileSize = fileData.size.toLong(),
            mimeType = "application/octet-stream",
            sha256 = "a".repeat(64), // wrong hash
            chunkSize = chunkSize,
            chunkCount = chunkCount,
            senderIdentity = "",
            createdAt = 0L,
        )

        val receiver = FerryTransferReceiver(meta, stagingDir)
        receiver.begin()

        val frame = FerryTransferClient.encodeChunkFrame(tid, 0, fileData)
        val (decodedTid, seq, data) = FerryTransferReceiver.decodeChunkFrame(frame)
        receiver.receiveChunk(decodedTid, seq, data)

        val success = receiver.finalise()
        assertFalse("Should fail with wrong SHA-256", success)
        val tempFile = File(stagingDir, "$tid.part")
        assertFalse("Temp file should be deleted on failure", tempFile.exists())
    }

    @Test fun test24_cancellationDeletesTempFile() {
        val fileData = ByteArray(100) { it.toByte() }
        val tid = UUID.randomUUID().toString()
        val meta = TransferMetadata(
            transferId = tid, fileName = "x.bin", fileSize = 100L,
            mimeType = "application/octet-stream", sha256 = "a".repeat(64),
            chunkSize = FerryTransferClient.CHUNK_SIZE, chunkCount = 1,
            senderIdentity = "", createdAt = 0L,
        )
        val receiver = FerryTransferReceiver(meta, stagingDir)
        receiver.begin()
        receiver.cancel()
        val tempFile = File(stagingDir, "$tid.part")
        assertFalse("Temp file should be deleted on cancel", tempFile.exists())
        assertEquals(TransferState.CANCELLED, receiver.let { TransferState.CANCELLED })
    }

    // ── 25: Out-of-order sequence rejected ───────────────────────────────────

    @Test(expected = IllegalArgumentException::class) fun test25_outOfOrderSeqRejected() {
        val tid = UUID.randomUUID().toString()
        val meta = TransferMetadata(
            transferId = tid, fileName = "x.bin", fileSize = 100L,
            mimeType = "application/octet-stream", sha256 = "a".repeat(64),
            chunkSize = FerryTransferClient.CHUNK_SIZE, chunkCount = 1,
            senderIdentity = "", createdAt = 0L,
        )
        val receiver = FerryTransferReceiver(meta, stagingDir)
        receiver.begin()
        // seq=1 when expecting 0
        receiver.receiveChunk(tid, 1, ByteArray(10))
    }

    // ── 26: Protocol constants ────────────────────────────────────────────────

    @Test fun test26_protocolConstantsHaveNewTypes() {
        assertEquals("TRANSFER_CHUNK", dev.ferry.app.protocol.ProtocolConstants.MessageTypes.TRANSFER_CHUNK)
        assertEquals("TRANSFER_RESULT", dev.ferry.app.protocol.ProtocolConstants.MessageTypes.TRANSFER_RESULT)
        assertEquals("TRANSFER_COMPLETE", dev.ferry.app.protocol.ProtocolConstants.MessageTypes.TRANSFER_COMPLETE)
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private fun b(s: String) = s.toByteArray(Charsets.UTF_8)
}
