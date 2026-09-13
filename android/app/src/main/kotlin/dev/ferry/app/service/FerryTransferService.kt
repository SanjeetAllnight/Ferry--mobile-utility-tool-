package dev.ferry.app.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import dev.ferry.app.MainActivity

/**
 * Foreground service that keeps transfer I/O alive when Ferry is backgrounded.
 *
 * Lifecycle:
 *   - Started via [startTransferService] when a transfer begins.
 *   - Stopped via [stopTransferService] when the transfer completes or is cancelled.
 *   - Shows a sticky progress notification while active.
 *
 * No transfer logic lives here; the actual I/O runs in [FerryControlClient]'s
 * coroutine scope, which is application-scoped and survives activity transitions.
 * This service only exists to satisfy Android's foreground-service requirement.
 */
class FerryTransferService : Service() {

    companion object {
        private const val TAG = "FerryTransferService"
        private const val CHANNEL_ID = "ferry_transfer_channel"
        private const val NOTIF_ID = 1001

        const val ACTION_START = "dev.ferry.app.TRANSFER_START"
        const val ACTION_UPDATE = "dev.ferry.app.TRANSFER_UPDATE"
        const val ACTION_STOP  = "dev.ferry.app.TRANSFER_STOP"

        const val EXTRA_FILE_NAME  = "file_name"
        const val EXTRA_PROGRESS   = "progress_pct"
        const val EXTRA_BATCH_INFO = "batch_info"

        /** Start the foreground service to cover an active transfer. */
        fun startTransferService(context: Context, fileName: String) {
            val intent = Intent(context, FerryTransferService::class.java).apply {
                action = ACTION_START
                putExtra(EXTRA_FILE_NAME, fileName)
            }
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent)
                } else {
                    context.startService(intent)
                }
            } catch (e: Exception) {
                Log.w(TAG, "Could not start transfer service: ${e.message}")
            }
        }

        /** Update the notification progress (0-100). */
        fun updateProgress(context: Context, fileName: String, progressPct: Int, batchInfo: String = "") {
            val intent = Intent(context, FerryTransferService::class.java).apply {
                action = ACTION_UPDATE
                putExtra(EXTRA_FILE_NAME, fileName)
                putExtra(EXTRA_PROGRESS, progressPct)
                putExtra(EXTRA_BATCH_INFO, batchInfo)
            }
            try { context.startService(intent) } catch (e: Exception) { /* no-op if not running */ }
        }

        /** Stop the foreground service after a transfer finishes. */
        fun stopTransferService(context: Context) {
            val intent = Intent(context, FerryTransferService::class.java).apply {
                action = ACTION_STOP
            }
            try { context.startService(intent) } catch (e: Exception) { /* no-op */ }
        }
    }

    private var currentFileName: String = "File"
    private var currentProgressPct: Int = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        Log.i(TAG, "FerryTransferService created")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> {
                currentFileName = intent.getStringExtra(EXTRA_FILE_NAME) ?: "File"
                currentProgressPct = 0
                startForeground(NOTIF_ID, buildNotification())
                Log.i(TAG, "Transfer service started for: $currentFileName")
            }
            ACTION_UPDATE -> {
                currentFileName = intent.getStringExtra(EXTRA_FILE_NAME) ?: currentFileName
                currentProgressPct = intent.getIntExtra(EXTRA_PROGRESS, currentProgressPct)
                val batchInfo = intent.getStringExtra(EXTRA_BATCH_INFO) ?: ""
                updateNotification(batchInfo)
            }
            ACTION_STOP -> {
                Log.i(TAG, "Transfer service stopping")
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
            else -> {
                // Unknown intent; start with default notification to satisfy foreground requirement
                startForeground(NOTIF_ID, buildNotification())
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        super.onDestroy()
        Log.i(TAG, "FerryTransferService destroyed")
    }

    // ── Notification helpers ──────────────────────────────────────────────────

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "Ferry Transfers",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Shown while Ferry is transferring files"
                setShowBadge(false)
                setSound(null, null)
            }
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(subtitle: String = ""): Notification {
        val openIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val title = if (subtitle.isNotEmpty()) subtitle else "Transferring: $currentFileName"

        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle(title)
            .setContentText("$currentProgressPct% complete")
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setProgress(100, currentProgressPct, currentProgressPct == 0)
            .setOnlyAlertOnce(true)
            .build()
    }

    private fun updateNotification(batchInfo: String = "") {
        val nm = getSystemService(NotificationManager::class.java)
        nm?.notify(NOTIF_ID, buildNotification(batchInfo))
    }
}
