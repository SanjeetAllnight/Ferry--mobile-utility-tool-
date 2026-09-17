package dev.ferry.app.notification

import android.app.Notification
import android.content.ComponentName
import android.content.Intent
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Ferry Notification Listener Service.
 *
 * Receives Android system notification events and routes them through the
 * Ferry notification pipeline (filter → ID → dedup → dispatch).
 *
 * IMPORTANT design constraints:
 * - No network / blocking I/O on the listener callback thread.
 * - Framework Notification/Bundle objects stay inside this class only.
 * - Only plain NotificationDTO crosses the boundary to the dispatcher.
 * - Notification title/body must NEVER be written to logs.
 */
class FerryNotificationListenerService : NotificationListenerService() {

    companion object {
        private const val TAG = "FerryNLS"

        // Shared mutable state for UI reflection
        private val _listenerActive = MutableStateFlow(false)
        val listenerActive: StateFlow<Boolean> = _listenerActive.asStateFlow()

        // Single shared deduplicator instance
        val deduplicator = NotificationDeduplicator()

        // Dispatcher injected from MainActivity / Application after client is ready
        @Volatile var dispatcher: NotificationDispatcher? = null
    }

    override fun onListenerConnected() {
        Log.i(TAG, "Notification listener connected")
        _listenerActive.value = true
    }

    override fun onListenerDisconnected() {
        Log.w(TAG, "Notification listener disconnected — requesting rebind")
        _listenerActive.value = false
        requestRebind(ComponentName(this, FerryNotificationListenerService::class.java))
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        sbn ?: return

        // Check global feature gate
        if (!NotificationSettingsStore.isEnabled(applicationContext)) return

        // Apply filter
        if (!NotificationFilter.shouldMirror(sbn)) return

        val disp = dispatcher ?: return

        // Map to DTO — all framework object references end here
        val extras   = sbn.notification.extras
        val title    = extras?.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""
        val body     = extras?.getCharSequence(Notification.EXTRA_TEXT)?.toString()  ?: ""
        val ferryId  = FerryIdGenerator.ferryId(applicationContext, sbn.key)
        val appLabel = try {
            packageManager.getApplicationLabel(
                packageManager.getApplicationInfo(sbn.packageName, 0)
            ).toString()
        } catch (_: Exception) { sbn.packageName }

        // Deduplicate — drop if identical content was already sent
        if (!deduplicator.shouldSend(ferryId, title, body)) return

        val dto = NotificationDTO(
            ferryId     = ferryId,
            packageName = sbn.packageName,
            appLabel    = appLabel,
            title       = title,
            body        = body,
            postedAt    = sbn.postTime,
            category    = sbn.notification.category ?: "",
        )

        disp.enqueue(dto)
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?, rankingMap: android.service.notification.NotificationListenerService.RankingMap?, reason: Int) {
        sbn ?: return
        if (!NotificationSettingsStore.isEnabled(applicationContext)) return
        val disp = dispatcher ?: return
        val ferryId = FerryIdGenerator.ferryId(applicationContext, sbn.key)
        deduplicator.remove(ferryId)
        disp.enqueueRemove(ferryId)
    }
}
