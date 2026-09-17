package dev.ferry.app.notification

import android.util.Log
import dev.ferry.app.net.FerryControlClient
import dev.ferry.app.protocol.ProtocolConstants
import org.json.JSONObject

private const val TAG = "NotificationDispatcher"
private const val BURST_MAX   = 20
private const val BURST_TTL_MS = 60_000L   // 60 seconds

private const val NOTIF_MAX_TITLE = 200
private const val NOTIF_MAX_BODY  = 1000
private const val NOTIF_MAX_PKG   = 255
private const val NOTIF_MAX_LABEL = 100

/**
 * Dispatches mirrored Android notification DTOs over the active Ferry session.
 *
 * - On send: if a session is ESTABLISHED, sends immediately.
 * - If no session: buffers up to BURST_MAX entries for up to BURST_TTL_MS.
 *   Oldest entries are dropped if the buffer is full.
 * - No offline queue; no replay after reconnect.
 * - Does NOT maintain a permanent socket. Relies entirely on the existing
 *   FerryControlClient session.
 */
class NotificationDispatcher(private val client: FerryControlClient) {

    private data class BufferedEntry(val dto: NotificationDTO, val enqueuedAt: Long)

    private val buffer = ArrayDeque<BufferedEntry>()

    /**
     * Enqueue a notification for dispatch.
     * If the session is live, sends immediately and flushes the burst buffer.
     * Otherwise adds to the burst buffer (dropping oldest if full).
     */
    @Synchronized
    fun enqueue(dto: NotificationDTO) {
        purgeExpired()

        if (client.sessionState.value == dev.ferry.app.security.FerrySession.State.ESTABLISHED
            && client.peerSupportsNotify) {
            // Flush any buffered entries first
            val toFlush = buffer.toList()
            buffer.clear()
            for (entry in toFlush) {
                send(entry.dto)
            }
            send(dto)
        } else {
            // Buffer for when a session becomes available
            if (buffer.size >= BURST_MAX) {
                buffer.removeFirst()  // drop oldest
            }
            buffer.addLast(BufferedEntry(dto, System.currentTimeMillis()))
        }
    }

    /**
     * Flush the burst buffer to the current session, if available.
     * Call this after session ESTABLISHED.
     */
    @Synchronized
    fun flush() {
        purgeExpired()
        if (client.sessionState.value != dev.ferry.app.security.FerrySession.State.ESTABLISHED
            || !client.peerSupportsNotify) return

        val toFlush = buffer.toList()
        buffer.clear()
        for (entry in toFlush) {
            send(entry.dto)
        }
    }

    /** Send a REMOVE for a ferry_id (if session is live). */
    @Synchronized
    fun enqueueRemove(ferryId: String) {
        // Remove from buffer too
        buffer.removeAll { it.dto.ferryId == ferryId }

        if (client.sessionState.value != dev.ferry.app.security.FerrySession.State.ESTABLISHED
            || !client.peerSupportsNotify) return

        client.sendNotificationRemove(ferryId)
    }

    private fun send(dto: NotificationDTO) {
        if (dto.isRemove) {
            client.sendNotificationRemove(dto.ferryId)
            return
        }
        // Enforce truncation limits
        val safeTitle = dto.title.take(NOTIF_MAX_TITLE)
        val safeBody  = dto.body.take(NOTIF_MAX_BODY)
        val safePkg   = dto.packageName.take(NOTIF_MAX_PKG)
        val safeLabel = dto.appLabel.take(NOTIF_MAX_LABEL)

        client.sendNotificationPost(
            ferryId   = dto.ferryId,
            packageName = safePkg,
            appLabel  = safeLabel,
            title     = safeTitle,
            body      = safeBody,
            postedAt  = dto.postedAt,
            category  = dto.category,
        )
    }

    private fun purgeExpired() {
        val cutoff = System.currentTimeMillis() - BURST_TTL_MS
        buffer.removeAll { it.enqueuedAt < cutoff }
    }
}
