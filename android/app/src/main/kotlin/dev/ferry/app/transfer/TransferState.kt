package dev.ferry.app.transfer

import java.util.UUID

/**
 * Transfer state machine for Ferry Phase 3A.
 *
 * Both the sender (FerryTransferClient) and receiver (FerryTransferReceiver)
 * use this enum to track their local state.  The service layer manages the
 * correlated control-plane message exchange (TRANSFER_REQUEST/ACCEPT/etc.).
 */
enum class TransferState {
    IDLE,
    REQUESTED,      // TRANSFER_REQUEST sent or received; awaiting decision
    ACCEPTED,       // TRANSFER_ACCEPT exchanged; ready to stream
    TRANSFERRING,   // Chunks flowing
    CANCELLING,     // TRANSFER_CANCEL sent; draining
    COMPLETED,      // SHA-256 verified and file finalised
    FAILED,         // Error (IO, integrity, protocol)
    CANCELLED;      // Cancelled by either side
}

/**
 * Metadata for one file transfer, parsed from a TRANSFER_REQUEST payload.
 *
 * All fields are validated before any filesystem use.  The file_name is
 * treated as a basename; path separators are stripped before use.
 */
data class TransferMetadata(
    val transferId: String,
    val fileName: String,       // sanitised basename only
    val fileSize: Long,
    val mimeType: String,
    val sha256: String,         // hex SHA-256 of complete file
    val chunkSize: Int,
    val chunkCount: Int,
    val senderIdentity: String, // base64url Ed25519 public key
    val createdAt: Long,
    val protocolVersion: Int = 1,
) {
    companion object {
        const val MAX_FILE_NAME_BYTES = 255
        const val MAX_CHUNK_SIZE = 65536       // 64 KiB
        const val MAX_FILE_SIZE = 10L * 1024 * 1024 * 1024  // 10 GiB

        /**
         * Parse and validate a TRANSFER_REQUEST JSON payload.
         * Throws [IllegalArgumentException] on any validation failure.
         */
        fun fromJson(obj: org.json.JSONObject): TransferMetadata = fromMap(
            mapOf(
                "transfer_id" to obj.optString("transfer_id", ""),
                "file_name" to obj.optString("file_name", ""),
                "file_size" to obj.optLong("file_size", 0L),
                "mime_type" to obj.optString("mime_type", "application/octet-stream"),
                "sha256" to obj.optString("sha256", ""),
                "chunk_size" to obj.optInt("chunk_size", MAX_CHUNK_SIZE),
                "chunk_count" to obj.optInt("chunk_count", -1),
                "sender_identity" to obj.optString("sender_identity", ""),
                "created_at" to obj.optLong("created_at", System.currentTimeMillis()),
                "protocol_version" to obj.optInt("protocol_version", 1),
            )
        )

        /**
         * Validate and construct TransferMetadata from a plain map of field values.
         * This is the canonical validation logic — fromJson() delegates here.
         * Testable without Android org.json mocking.
         */
        fun fromMap(data: Map<String, Any?>): TransferMetadata {
            val transferId = data["transfer_id"] as? String ?: ""
            require(transferId.isNotEmpty()) { "transfer_id is empty" }
            // Validate UUID format
            UUID.fromString(transferId)

            val rawName = data["file_name"] as? String ?: ""
            val fileName = sanitiseFilename(rawName)
            require(fileName.isNotEmpty()) {
                "Empty or invalid file_name after sanitisation: '$rawName'"
            }

            val fileSize = when (val v = data["file_size"]) {
                is Long -> v
                is Int -> v.toLong()
                is Number -> v.toLong()
                else -> 0L
            }
            require(fileSize >= 0) { "file_size must be non-negative, got $fileSize" }
            require(fileSize <= MAX_FILE_SIZE) { "file_size $fileSize exceeds 10 GiB limit" }

            val chunkSize = when (val v = data["chunk_size"]) {
                is Int -> v
                is Number -> v.toInt()
                else -> MAX_CHUNK_SIZE
            }
            require(chunkSize in 1..MAX_CHUNK_SIZE) {
                "chunk_size $chunkSize out of range (1..$MAX_CHUNK_SIZE)"
            }

            val chunkCount = when (val v = data["chunk_count"]) {
                is Int -> v
                is Number -> v.toInt()
                else -> -1
            }
            val expectedChunks = if (fileSize > 0) ((fileSize + chunkSize - 1) / chunkSize).toInt() else 0
            require(chunkCount == expectedChunks) {
                "chunk_count $chunkCount does not match file_size/chunk_size (expected $expectedChunks)"
            }

            val sha256 = (data["sha256"] as? String) ?: ""
            require(sha256.length == 64 && sha256.all { it in '0'..'9' || it in 'a'..'f' || it in 'A'..'F' }) {
                "sha256 is not a valid 64-char hex string: '$sha256'"
            }

            return TransferMetadata(
                transferId = transferId,
                fileName = fileName,
                fileSize = fileSize,
                mimeType = (data["mime_type"] as? String) ?: "application/octet-stream",
                sha256 = sha256.lowercase(),
                chunkSize = chunkSize,
                chunkCount = chunkCount,
                senderIdentity = (data["sender_identity"] as? String) ?: "",
                createdAt = when (val v = data["created_at"]) {
                    is Long -> v
                    is Number -> v.toLong()
                    else -> System.currentTimeMillis()
                },
                protocolVersion = when (val v = data["protocol_version"]) {
                    is Int -> v
                    is Number -> v.toInt()
                    else -> 1
                },
            )
        }

        /**
         * Sanitise a remote-supplied filename to a safe basename.
         *
         * Strips all path components, control characters, and rejects
         * reserved names.  Returns empty string if the name cannot be made safe.
         */
        fun sanitiseFilename(raw: String): String {
            // Normalise path separators and extract basename
            var name = raw.replace("\\", "/").replace("\u0000", "")
            name = name.substringAfterLast("/")
            name = name.trimStart('.', '/')
            // Strip control characters (ASCII 0-31, 127)
            name = name.filter { it.code >= 32 && it.code != 127 }
            // Truncate to MAX_FILE_NAME_BYTES (UTF-8)
            val bytes = name.toByteArray(Charsets.UTF_8)
            if (bytes.size > MAX_FILE_NAME_BYTES) {
                name = String(bytes.copyOf(MAX_FILE_NAME_BYTES), Charsets.UTF_8)
            }
            // Reject dot-only or empty names
            if (name.isBlank() || name == "." || name == "..") return ""
            // Reject Windows/DOS reserved names
            val stem = name.substringBefore('.').uppercase()
            val reserved = setOf(
                "CON", "PRN", "AUX", "NUL",
                "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
            )
            if (stem in reserved) return ""
            return name
        }
    }

    fun toJson(): org.json.JSONObject = org.json.JSONObject().apply {
        put("transfer_id", transferId)
        put("file_name", fileName)
        put("file_size", fileSize)
        put("mime_type", mimeType)
        put("sha256", sha256)
        put("chunk_size", chunkSize)
        put("chunk_count", chunkCount)
        put("sender_identity", senderIdentity)
        put("created_at", createdAt)
        put("protocol_version", protocolVersion)
    }
}
