package dev.ferry.app.transfer

import android.util.Log
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.UUID

/**
 * FerryTransferReceiver — Incoming file transfer receiver (Android side).
 *
 * Processes binary TRANSFER_CHUNK frames decoded from the authenticated AEAD
 * session and writes them to a temporary file.  After all chunks are received
 * (signalled by TRANSFER_COMPLETE), verifies SHA-256 and atomically renames
 * the temp file to its final destination.
 *
 * File safety:
 * - Chunk payloads are written to <stagingDir>/<transferId>.part
 * - The final file is only placed at <stagingDir>/<sanitisedFileName> after
 *   SHA-256 verification passes.
 * - If verification fails, the .part file is deleted.
 * - No incomplete file is ever visible to the user.
 *
 * Remote-supplied filenames are sanitised by TransferMetadata.sanitiseFilename()
 * before any filesystem use.
 */
class FerryTransferReceiver(
    private val meta: TransferMetadata,
    private val stagingDir: File,
) {
    companion object {
        private const val TAG = "FerryTransferReceiver"

        /** FYCH binary magic — identifies a TRANSFER_CHUNK plaintext frame. */
        val CHUNK_MAGIC = byteArrayOf(0x46, 0x59, 0x43, 0x48) // "FYCH"
        private const val CHUNK_HEADER_SIZE = 4 + 16 + 4 + 4   // 28 bytes

        /**
         * Returns true if this decrypted plaintext is a TRANSFER_CHUNK binary frame
         * (starts with FYCH magic), rather than a JSON control envelope.
         */
        fun isChunkFrame(plaintext: ByteArray): Boolean {
            if (plaintext.size < 4) return false
            return plaintext[0] == CHUNK_MAGIC[0]
                    && plaintext[1] == CHUNK_MAGIC[1]
                    && plaintext[2] == CHUNK_MAGIC[2]
                    && plaintext[3] == CHUNK_MAGIC[3]
        }

        /**
         * Decode a TRANSFER_CHUNK binary plaintext.
         *
         * @return Triple(transferId, seqNumber, chunkData)
         * @throws IllegalArgumentException on structural or bounds violations
         */
        fun decodeChunkFrame(plaintext: ByteArray): Triple<String, Int, ByteArray> {
            require(plaintext.size >= CHUNK_HEADER_SIZE) {
                "TRANSFER_CHUNK too short: ${plaintext.size} < $CHUNK_HEADER_SIZE"
            }
            val magic = plaintext.copyOfRange(0, 4)
            require(magic.contentEquals(CHUNK_MAGIC)) {
                "Bad TRANSFER_CHUNK magic: ${magic.map { it.toInt().and(0xFF).toString(16) }}"
            }

            val buf = ByteBuffer.wrap(plaintext).order(ByteOrder.BIG_ENDIAN)
            buf.position(4) // skip magic

            val msb = buf.getLong()
            val lsb = buf.getLong()
            val transferId = UUID(msb, lsb).toString()
            val seq = buf.getInt()
            val payloadLen = buf.getInt()

            require(payloadLen <= FerryTransferClient.CHUNK_SIZE) {
                "Remote chunk payloadLen $payloadLen > MAX_CHUNK_SIZE ${FerryTransferClient.CHUNK_SIZE}"
            }
            require(plaintext.size >= CHUNK_HEADER_SIZE + payloadLen) {
                "TRANSFER_CHUNK truncated: need ${CHUNK_HEADER_SIZE + payloadLen}, have ${plaintext.size}"
            }

            val data = plaintext.copyOfRange(CHUNK_HEADER_SIZE, CHUNK_HEADER_SIZE + payloadLen)
            return Triple(transferId, seq, data)
        }
    }

    private var state: TransferState = TransferState.IDLE
    private val tempFile: File = File(stagingDir, "${meta.transferId}.part")
    private val digest: MessageDigest = MessageDigest.getInstance("SHA-256")
    private var bytesReceived: Long = 0L
    private var nextSeq: Int = 0

    val bytesReceivedTotal: Long get() = bytesReceived

    /**
     * Open the temp file and transition to TRANSFERRING state.
     * Must be called after TRANSFER_ACCEPT is sent.
     */
    fun begin() {
        check(state == TransferState.IDLE) { "begin() called in state $state" }
        stagingDir.mkdirs()
        tempFile.createNewFile()
        state = TransferState.TRANSFERRING
        Log.i(TAG, "Receiving transfer ${meta.transferId.take(8)}: ${meta.fileName} (${meta.fileSize} bytes)")
    }

    /**
     * Process one decoded chunk.
     *
     * @throws IllegalArgumentException on sequence mismatch, wrong transfer_id, or oversized data.
     * @throws IllegalStateException if not in TRANSFERRING state.
     */
    fun receiveChunk(transferId: String, seq: Int, data: ByteArray) {
        check(state == TransferState.TRANSFERRING) {
            "Cannot receive chunk in state $state"
        }
        require(transferId == meta.transferId) {
            "Chunk transfer_id mismatch: expected ${meta.transferId}, got $transferId"
        }
        require(seq == nextSeq) {
            "Out-of-order chunk: expected seq $nextSeq, got $seq"
        }
        require(data.size <= meta.chunkSize) {
            "Chunk payload ${data.size} > declared chunkSize ${meta.chunkSize}"
        }

        tempFile.appendBytes(data)
        digest.update(data)
        bytesReceived += data.size
        nextSeq++
    }

    /**
     * Verify SHA-256 and atomically rename temp file to final destination.
     *
     * @return true if integrity verification passed and the file was finalised.
     *         false if the hash failed — temp file is deleted.
     */
    fun finalise(): Boolean {
        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        val expected = meta.sha256.lowercase()

        if (actual != expected) {
            Log.e(TAG, "Integrity mismatch for ${meta.transferId.take(8)}: expected=$expected, actual=$actual")
            cleanup()
            state = TransferState.FAILED
            return false
        }

        // Atomic rename to final destination
        var finalFile = File(stagingDir, meta.fileName)
        if (finalFile.exists()) {
            finalFile = File(stagingDir, "${meta.transferId.take(8)}_${meta.fileName}")
        }
        val renamed = tempFile.renameTo(finalFile)
        if (!renamed) {
            Log.e(TAG, "Failed to rename ${tempFile.name} to ${finalFile.name}")
            cleanup()
            state = TransferState.FAILED
            return false
        }

        state = TransferState.COMPLETED
        Log.i(TAG, "Transfer ${meta.transferId.take(8)} complete → ${finalFile.name} (SHA-256 verified)")
        return true
    }

    /**
     * Cancel the transfer and remove the temp file.
     */
    fun cancel() {
        cleanup()
        state = TransferState.CANCELLED
        Log.i(TAG, "Transfer ${meta.transferId.take(8)} cancelled, temp file removed")
    }

    private fun cleanup() {
        if (tempFile.exists()) {
            tempFile.delete()
        }
    }
}
