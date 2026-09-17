package dev.ferry.app.notification

import android.content.Context

private const val PREF_NAME = "ferry_notification_settings"
private const val KEY_ENABLED = "notify_mirroring_enabled"

/**
 * SharedPreferences backing store for notification mirroring settings.
 *
 * Default: OFF (disabled). User must opt in explicitly after reading
 * the consent explanation.
 */
object NotificationSettingsStore {

    fun isEnabled(context: Context): Boolean =
        context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
            .getBoolean(KEY_ENABLED, false)

    fun setEnabled(context: Context, enabled: Boolean) {
        context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
            .edit()
            .putBoolean(KEY_ENABLED, enabled)
            .apply()
    }
}
