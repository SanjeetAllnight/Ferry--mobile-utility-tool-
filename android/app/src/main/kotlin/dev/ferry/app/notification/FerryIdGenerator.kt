package dev.ferry.app.notification

import android.content.Context
import android.util.Base64
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

private const val PREF_NAME = "ferry_notification_id"
private const val KEY_SALT   = "ferry_id_salt"
private const val HMAC_ALGO  = "HmacSHA256"
private const val OUTPUT_BYTES = 16

/**
 * Derives a stable, per-installation Ferry notification ID from an Android SBN key.
 *
 * ferry_id = HMAC-SHA256(salt, sbn.key) truncated to 16 bytes, encoded as base64url.
 *
 * The raw Android notification key is NEVER transmitted over the network.
 * The salt is generated once per installation and stored in SharedPreferences.
 */
object FerryIdGenerator {

    @Volatile private var cachedSalt: ByteArray? = null

    fun ferryId(context: Context, sbnKey: String): String {
        val salt = getSalt(context)
        val mac = Mac.getInstance(HMAC_ALGO)
        mac.init(SecretKeySpec(salt, HMAC_ALGO))
        val full = mac.doFinal(sbnKey.toByteArray(Charsets.UTF_8))
        val truncated = full.copyOf(OUTPUT_BYTES)
        return Base64.encodeToString(truncated, Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP)
    }

    private fun getSalt(context: Context): ByteArray {
        cachedSalt?.let { return it }
        val prefs = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
        val stored = prefs.getString(KEY_SALT, null)
        if (stored != null) {
            val salt = Base64.decode(stored, Base64.DEFAULT)
            cachedSalt = salt
            return salt
        }
        val salt = ByteArray(32).also { SecureRandom().nextBytes(it) }
        prefs.edit().putString(KEY_SALT, Base64.encodeToString(salt, Base64.DEFAULT)).apply()
        cachedSalt = salt
        return salt
    }
}
