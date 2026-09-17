package dev.ferry.app.notification

import android.app.Notification
import android.service.notification.StatusBarNotification

private const val FERRY_PACKAGE = "dev.ferry.app"

/**
 * Stateless notification filter.
 *
 * Returns true when the notification should be mirrored to Linux.
 * All rejection reasons are logged at DEBUG level using only metadata
 * (package name, flags) — never notification content.
 */
object NotificationFilter {

    fun shouldMirror(sbn: StatusBarNotification): Boolean {
        val notification = sbn.notification ?: return false

        // Never mirror our own notifications
        if (sbn.packageName == FERRY_PACKAGE) return false

        // Never mirror group summaries
        if (notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return false

        // Never mirror ongoing / persistent notifications
        if (notification.flags and Notification.FLAG_ONGOING_EVENT != 0) return false

        // Never mirror non-clearable system-state notifications
        if (!sbn.isClearable) return false

        // Never mirror media / transport-control notifications
        val category = notification.category
        if (category == Notification.CATEGORY_TRANSPORT) return false

        // Filter empty title + body
        val extras = notification.extras
        val title = extras?.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
        val text  = extras?.getCharSequence(Notification.EXTRA_TEXT)?.toString()  ?: ""
        if (title.isBlank() && text.isBlank()) return false

        return true
    }
}
