package dev.ferry.app.notification

/**
 * Minimal notification data transfer object.
 *
 * Contains only the fields required by the Ferry notification mirroring spec.
 * title and body are user-visible SENSITIVE strings and must NEVER be written
 * to logs in any layer that processes this object.
 *
 * ferry_id is a stable per-installation HMAC-derived identifier — never the
 * raw Android notification key.
 */
data class NotificationDTO(
    val ferryId: String,
    val packageName: String,
    val appLabel: String,
    val title: String,    // SENSITIVE — do not log
    val body: String,     // SENSITIVE — do not log
    val postedAt: Long,   // Unix epoch millis
    val category: String,
    val isRemove: Boolean = false,
) {
    /** Deliberately redacts sensitive fields. */
    override fun toString(): String =
        "NotificationDTO(ferryId=$ferryId, package=$packageName, appLabel=$appLabel, postedAt=$postedAt)"
}
