package dev.ferry.app.notification

import java.security.MessageDigest

private const val MAX_ENTRIES = 200

/**
 * Content-based deduplicator for mirrored notifications.
 *
 * Tracks the last content hash (SHA-256 of title+body) per ferry_id.
 * Returns true if the notification should be sent (new or changed content).
 *
 * Uses an insertion-ordered LinkedHashMap as an LRU cache bounded at MAX_ENTRIES.
 * Thread-safe via @Synchronized.
 */
class NotificationDeduplicator {

    private val cache = object : LinkedHashMap<String, String>(MAX_ENTRIES + 1, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, String>?): Boolean =
            size > MAX_ENTRIES
    }

    @Synchronized
    fun shouldSend(ferryId: String, title: String, body: String): Boolean {
        val hash = contentHash(title, body)
        val prev = cache[ferryId]
        cache[ferryId] = hash
        return prev != hash
    }

    @Synchronized
    fun remove(ferryId: String) {
        cache.remove(ferryId)
    }

    private fun contentHash(title: String, body: String): String {
        val md = MessageDigest.getInstance("SHA-256")
        md.update(title.toByteArray(Charsets.UTF_8))
        md.update(0.toByte())
        md.update(body.toByteArray(Charsets.UTF_8))
        return md.digest().joinToString("") { "%02x".format(it) }
    }
}
