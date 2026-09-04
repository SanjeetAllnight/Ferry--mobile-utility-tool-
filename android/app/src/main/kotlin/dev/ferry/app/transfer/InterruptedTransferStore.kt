package dev.ferry.app.transfer

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * InterruptedTransferStore — Phase 3E Android-side persistence for interrupted transfers.
 *
 * Stores interrupted transfer records in SharedPreferences so that they survive
 * process death and can be presented to the user on the next peer connection.
 *
 * Each record is stored as a JSON blob under the key:
 *   "interrupted_<transfer_id>"
 *
 * An index of all known transfer IDs is maintained at "index" to support
 * efficient listing without scanning all SharedPreferences keys.
 *
 * TTL: Records expire after 7 days (matching the Linux side). Expired records
 * are pruned on load.
 *
 * Thread safety: All operations are synchronised on the SharedPreferences object
 * via commit/apply. Read operations are safe from any thread.
 */
class InterruptedTransferStore(context: Context) {

    companion object {
        private const val TAG = "InterruptedTransferStore"
        private const val PREFS_NAME = "ferry_interrupted_transfers"
        private const val KEY_INDEX = "index"
        private const val KEY_PREFIX = "interrupted_"

        /** 7 days in milliseconds — matches Linux expire_after_days = 7 */
        val TTL_MS: Long = TimeUnit.DAYS.toMillis(7)

        fun makeExpireAt(nowMs: Long): Long = nowMs + TTL_MS
    }

    private val prefs: SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    // ── Data class ───────────────────────────────────────────────────────────

    data class InterruptedRecord(
        val transferId: String,
        val fileName: String,
        val fileSize: Long,
        val mimeType: String,
        val sha256: String,
        val chunkSize: Int,
        val chunkCount: Int,
        val senderIdentity: String,     // sender's public key (base64) if Android is receiver
        val receiverIdentity: String,   // receiver's public key (base64) if Android is sender
        val originalMetadataJson: String,
        val bytesReceived: Long,
        val resumeChunkIndex: Int,
        val partialSha256: String,      // SHA-256 of bytes already received
        val interruptedAt: Long,        // epoch ms
        val expireAt: Long,             // epoch ms
        val direction: Direction,       // INCOMING (Android was receiver) or OUTGOING (Android was sender)
        val contentUriString: String?,  // For OUTGOING: SAF URI of source file; null for INCOMING
    ) {
        enum class Direction { INCOMING, OUTGOING }

        fun toJson(): JSONObject = JSONObject().apply {
            put("transfer_id", transferId)
            put("file_name", fileName)
            put("file_size", fileSize)
            put("mime_type", mimeType)
            put("sha256", sha256)
            put("chunk_size", chunkSize)
            put("chunk_count", chunkCount)
            put("sender_identity", senderIdentity)
            put("receiver_identity", receiverIdentity)
            put("original_metadata_json", originalMetadataJson)
            put("bytes_received", bytesReceived)
            put("resume_chunk_index", resumeChunkIndex)
            put("partial_sha256", partialSha256)
            put("interrupted_at", interruptedAt)
            put("expire_at", expireAt)
            put("direction", direction.name)
            put("content_uri_string", contentUriString ?: JSONObject.NULL)
        }

        companion object {
            fun fromJson(json: JSONObject): InterruptedRecord = InterruptedRecord(
                transferId = json.getString("transfer_id"),
                fileName = json.getString("file_name"),
                fileSize = json.getLong("file_size"),
                mimeType = json.optString("mime_type", "application/octet-stream"),
                sha256 = json.getString("sha256"),
                chunkSize = json.getInt("chunk_size"),
                chunkCount = json.getInt("chunk_count"),
                senderIdentity = json.optString("sender_identity", ""),
                receiverIdentity = json.optString("receiver_identity", ""),
                originalMetadataJson = json.optString("original_metadata_json", "{}"),
                bytesReceived = json.getLong("bytes_received"),
                resumeChunkIndex = json.getInt("resume_chunk_index"),
                partialSha256 = json.optString("partial_sha256", ""),
                interruptedAt = json.getLong("interrupted_at"),
                expireAt = json.getLong("expire_at"),
                direction = Direction.valueOf(json.optString("direction", Direction.INCOMING.name)),
                contentUriString = if (json.isNull("content_uri_string")) null
                                   else json.optString("content_uri_string"),
            )
        }
    }

    // ── Write operations ─────────────────────────────────────────────────────

    /**
     * Persist an interrupted transfer record.
     * If a record with the same transferId already exists, it is replaced.
     */
    fun save(record: InterruptedRecord) {
        val key = KEY_PREFIX + record.transferId
        val json = record.toJson().toString()
        prefs.edit()
            .putString(key, json)
            .apply()
        addToIndex(record.transferId)
        Log.d(
            TAG,
            "Saved interrupted transfer ${record.transferId.take(8)} " +
                "(${record.direction}, ${record.bytesReceived} bytes)"
        )
    }

    /**
     * Delete a single interrupted transfer record.
     * Idempotent — safe to call if the record does not exist.
     */
    fun delete(transferId: String) {
        val key = KEY_PREFIX + transferId
        prefs.edit().remove(key).apply()
        removeFromIndex(transferId)
        Log.d(TAG, "Deleted interrupted transfer ${transferId.take(8)}")
    }

    /**
     * Clear all interrupted transfer records.
     */
    fun clear() {
        val index = loadIndex()
        val editor = prefs.edit()
        for (tid in index) {
            editor.remove(KEY_PREFIX + tid)
        }
        editor.putString(KEY_INDEX, "").apply()
        Log.d(TAG, "Cleared all ${index.size} interrupted transfer records")
    }

    // ── Read operations ──────────────────────────────────────────────────────

    /**
     * Load a single interrupted transfer by transferId.
     * Returns null if not found or expired.
     */
    fun get(transferId: String): InterruptedRecord? {
        val json = prefs.getString(KEY_PREFIX + transferId, null) ?: return null
        return try {
            val record = InterruptedRecord.fromJson(JSONObject(json))
            if (isExpired(record)) {
                delete(transferId)
                null
            } else {
                record
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to parse interrupted record $transferId: ${e.message}")
            null
        }
    }

    /**
     * List all non-expired interrupted transfers from a given peer (by identity).
     *
     * Pass [senderIdentity] for INCOMING records (peer was sender).
     * Pass null to list all non-expired records regardless of peer.
     *
     * Stale/expired records are pruned automatically.
     */
    fun listForSender(senderIdentity: String? = null): List<InterruptedRecord> {
        val index = loadIndex()
        val result = mutableListOf<InterruptedRecord>()
        val toDelete = mutableListOf<String>()

        for (tid in index) {
            val raw = prefs.getString(KEY_PREFIX + tid, null) ?: continue
            try {
                val record = InterruptedRecord.fromJson(JSONObject(raw))
                if (isExpired(record)) {
                    toDelete.add(tid)
                    continue
                }
                if (senderIdentity == null || record.senderIdentity == senderIdentity) {
                    result.add(record)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Skipping corrupt interrupted record $tid: ${e.message}")
                toDelete.add(tid)
            }
        }

        // Prune expired/corrupt records.
        if (toDelete.isNotEmpty()) {
            val editor = prefs.edit()
            for (tid in toDelete) {
                editor.remove(KEY_PREFIX + tid)
            }
            editor.apply()
            val remainingIndex = (index - toDelete.toSet())
            saveIndex(remainingIndex)
            Log.d(TAG, "Pruned ${toDelete.size} expired interrupted records")
        }

        return result
    }

    /**
     * Remove all records that have passed their expiry time.
     * @return number of records removed.
     */
    fun pruneExpired(): Int {
        val index = loadIndex()
        val toDelete = mutableListOf<String>()

        for (tid in index) {
            val raw = prefs.getString(KEY_PREFIX + tid, null)
            if (raw == null) {
                toDelete.add(tid)
                continue
            }
            try {
                val record = InterruptedRecord.fromJson(JSONObject(raw))
                if (isExpired(record)) toDelete.add(tid)
            } catch (e: Exception) {
                toDelete.add(tid)
            }
        }

        if (toDelete.isNotEmpty()) {
            val editor = prefs.edit()
            for (tid in toDelete) editor.remove(KEY_PREFIX + tid)
            editor.apply()
            saveIndex(index - toDelete.toSet())
        }
        Log.d(TAG, "pruneExpired: removed ${toDelete.size} records")
        return toDelete.size
    }

    // ── Private helpers ──────────────────────────────────────────────────────

    private fun isExpired(record: InterruptedRecord): Boolean =
        System.currentTimeMillis() > record.expireAt

    private fun loadIndex(): Set<String> {
        val raw = prefs.getString(KEY_INDEX, "") ?: return emptySet()
        return if (raw.isBlank()) emptySet()
        else raw.split(",").filter { it.isNotBlank() }.toSet()
    }

    private fun saveIndex(index: Set<String>) {
        prefs.edit().putString(KEY_INDEX, index.joinToString(",")).apply()
    }

    private fun addToIndex(transferId: String) {
        val current = loadIndex().toMutableSet()
        if (current.add(transferId)) saveIndex(current)
    }

    private fun removeFromIndex(transferId: String) {
        val current = loadIndex().toMutableSet()
        if (current.remove(transferId)) saveIndex(current)
    }
}
