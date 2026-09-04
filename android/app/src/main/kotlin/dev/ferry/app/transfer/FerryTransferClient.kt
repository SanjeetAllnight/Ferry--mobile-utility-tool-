package dev.ferry.app.transfer

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.InputStream
import java.io.OutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.UUID

/**
 * FerryTransferClient — Outgoing file transfer sender (Android side).
 *
 * Streams file content in 64 KiB chunks over the existing authenticated
 * AEAD-encrypted control session.  Each chunk is encoded as a binary
 * TRANSFER_CHUNK frame starting with "FYCH" magic.
 *
 * The caller (FerryControlClient) is responsible for:
 *   1. Sending TRANSFER_REQUEST (metadata) over the encrypted control channel
 *   2. Waiting for TRANSFER_ACCEPT
 *   3. Calling streamChunks() / streamChunksFromStream() with the OutputStream and file content
 *   4. Sending TRANSFER_COMPLETE after streaming returns
 *   5. Waiting for TRANSFER_RESULT
 *
 * This class handles ONLY the data-plane chunk encoding and streaming.
 *
 * Security: all output bytes are passed through the caller's encrypt_frame()
 * equivalent (the FerrySession's AEAD cipher) before being written to the
 * socket.  No plaintext chunk data is written directly.
 *
 * IMPORTANT: This class does NOT hold a reference to the OutputStream between
 * calls.  It is stateless with respect to the transport.
 */
class FerryTransferClient {

    companion object {
        private const val TAG = "FerryTransferClient"

        /** FYCH binary magic — identifies a TRANSFER_CHUNK plaintext frame. */
        val CHUNK_MAGIC = byteArrayOf(0x46, 0x59, 0x43, 0x48) // "FYCH"

        /** Selected chunk payload size: 64 KiB. */
        const val CHUNK_SIZE = 65536

        /** Header: 4B magic + 16B UUID + 4B seq + 4B payload_len = 28 bytes */
        const val CHUNK_HEADER_SIZE = 4 + 16 + 4 + 4

        /**
         * Encode one TRANSFER_CHUNK binary frame.
         *
         * Layout:
         *   [4B "FYCH"] [16B UUID bytes] [4B seq uint32 BE] [4B payload_len uint32 BE] [data]
         */
        fun encodeChunkFrame(transferId: String, seq: Int, data: ByteArray): ByteArray {
            require(data.size <= CHUNK_SIZE) {
                "Chunk payload ${data.size} > MAX_CHUNK_SIZE $CHUNK_SIZE"
            }
            val uid = UUID.fromString(transferId)
            val buf = ByteBuffer.allocate(CHUNK_HEADER_SIZE + data.size).apply {
                order(ByteOrder.BIG_ENDIAN)
                put(CHUNK_MAGIC)
                putLong(uid.mostSignificantBits)
                putLong(uid.leastSignificantBits)
                putInt(seq)
                putInt(data.size)
                put(data)
            }
            return buf.array()
        }

        /**
         * Compute SHA-256 hex digest of a byte array.
         * Used for pre-computed hash of small in-memory content.
         */
        fun sha256Hex(data: ByteArray): String {
            val digest = MessageDigest.getInstance("SHA-256").digest(data)
            return digest.joinToString("") { "%02x".format(it) }
        }
    }

    /**
     * Stream file content from an in-memory byte array.
     * Kept for unit-test compatibility. Production code uses [streamChunksFromStream].
     *
     * @param transferId   Transfer UUID (from TransferMetadata)
     * @param fileBytes    Raw file content
     * @param encryptAndWrite   Callback that encrypts a raw chunk frame and writes it to the socket.
     * @param onProgress   Optional progress callback (bytesWritten, totalBytes)
     * @param cancelSignal Returns true if the transfer has been cancelled.
     * @return             True if streaming completed, false if cancelled.
     */
    suspend fun streamChunks(
        transferId: String,
        fileBytes: ByteArray,
        encryptAndWrite: suspend (ByteArray) -> Unit,
        onProgress: ((Long, Long) -> Unit)? = null,
        cancelSignal: () -> Boolean = { false },
    ): Boolean {
        val totalBytes = fileBytes.size.toLong()
        var seq = 0
        var offset = 0

        Log.i(TAG, "Streaming transfer $transferId: ${fileBytes.size} bytes")

        while (offset < fileBytes.size) {
            if (cancelSignal()) {
                Log.i(TAG, "Transfer $transferId cancelled at seq=$seq")
                return false
            }

            val end = minOf(offset + CHUNK_SIZE, fileBytes.size)
            val chunk = fileBytes.copyOfRange(offset, end)
            val frame = encodeChunkFrame(transferId, seq, chunk)
            encryptAndWrite(frame)

            offset = end
            seq++
            onProgress?.invoke(offset.toLong(), totalBytes)
        }

        Log.i(TAG, "Transfer $transferId: all $seq chunks sent")
        return true
    }

    /**
     * Stream file content from an [InputStream] without loading the full file into memory.
     *
     * Reads up to [CHUNK_SIZE] bytes per iteration, encodes each as a FYCH chunk frame,
     * encrypts it via [encryptAndWrite], and reports progress via [onProgress].
     * SHA-256 is computed incrementally.
     *
     * @param transferId      Transfer UUID
     * @param inputStream     Open InputStream positioned at the start of the file.
     *                        The caller is responsible for closing it.
     * @param totalBytes      Expected total file size in bytes (for progress reporting).
     * @param encryptAndWrite Suspending callback: receives a raw (unencrypted) chunk frame
     *                        and must encrypt-then-write it to the socket.
     * @param onProgress      Optional callback (bytesDone, totalBytes)
     * @param cancelSignal    Returns true if the transfer should be aborted.
     * @return                Pair of (success: Boolean, sha256Hex: String).
     *                        success is false if cancelled before all bytes were sent.
     */
    suspend fun streamChunksFromStream(
        transferId: String,
        inputStream: InputStream,
        totalBytes: Long,
        encryptAndWrite: suspend (ByteArray) -> Unit,
        onProgress: ((Long, Long) -> Unit)? = null,
        cancelSignal: () -> Boolean = { false },
    ): Pair<Boolean, String> = withContext(Dispatchers.IO) {
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(CHUNK_SIZE)
        var seq = 0
        var bytesSent = 0L

        Log.i(TAG, "Streaming transfer $transferId from InputStream: $totalBytes bytes expected")

        while (true) {
            if (cancelSignal()) {
                Log.i(TAG, "Transfer $transferId cancelled at seq=$seq")
                return@withContext Pair(false, "")
            }

            val bytesRead = inputStream.read(buffer, 0, CHUNK_SIZE)
            if (bytesRead == -1) break  // EOF

            val chunk = if (bytesRead == CHUNK_SIZE) buffer else buffer.copyOf(bytesRead)
            digest.update(chunk, 0, bytesRead)

            val frame = encodeChunkFrame(transferId, seq, chunk)
            encryptAndWrite(frame)

            bytesSent += bytesRead
            seq++
            onProgress?.invoke(bytesSent, totalBytes)
        }

        val sha256 = digest.digest().joinToString("") { "%02x".format(it) }
        Log.i(TAG, "Transfer $transferId: $seq chunks sent, sha256=$sha256")
        Pair(true, sha256)
    }
}
