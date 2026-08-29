package dev.ferry.app.net

import android.content.Context
import android.util.Log
import org.json.JSONObject

/**
 * FerryTrustStore persists trusted peer identity public keys on Android.
 *
 * Backed by SharedPreferences (private mode). Each entry stores:
 *   - deviceId (UUID string)
 *   - deviceName (display name)
 *   - identityPublicKeyB64 (base64url Ed25519 public key — this is the stable identity)
 *   - pairedAt (epoch ms)
 *   - lastSeen (epoch ms)
 *
 * Phase 2C: migrate to Room DB or EncryptedSharedPreferences for stronger at-rest protection.
 */
class FerryTrustStore(context: Context) {

    companion object {
        private const val TAG = "FerryTrustStore"
        private const val PREFS_NAME = "ferry_trust_store"
        // JSON array stored under this key
        private const val KEY_TRUSTED_PEERS = "trusted_peers"
    }

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    data class TrustedPeer(
        val deviceId: String,
        val deviceName: String,
        val identityPublicKeyB64: String,
        val pairedAt: Long,
        val lastSeen: Long,
    )

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    fun isKnownPeer(identityPublicKeyB64: String): Boolean =
        findByPublicKey(identityPublicKeyB64) != null

    fun findByPublicKey(identityPublicKeyB64: String): TrustedPeer? =
        listPeers().firstOrNull { it.identityPublicKeyB64 == identityPublicKeyB64 }

    fun persistTrust(deviceId: String, deviceName: String, identityPublicKeyB64: String) {
        val now = System.currentTimeMillis()
        val existing = findByPublicKey(identityPublicKeyB64)
        val peer = TrustedPeer(
            deviceId = deviceId.ifBlank { existing?.deviceId ?: deviceId },
            deviceName = deviceName,
            identityPublicKeyB64 = identityPublicKeyB64,
            pairedAt = existing?.pairedAt ?: now,
            lastSeen = now,
        )
        val peers = listPeers().toMutableList()
        peers.removeAll { it.identityPublicKeyB64 == identityPublicKeyB64 }
        peers.add(peer)
        savePeers(peers)
        Log.i(TAG, "Persisted trust for $deviceName (${deviceId.take(8)}...)")
    }

    fun updateLastSeen(identityPublicKeyB64: String) {
        val now = System.currentTimeMillis()
        val peers = listPeers().map { peer ->
            if (peer.identityPublicKeyB64 == identityPublicKeyB64) {
                peer.copy(lastSeen = now)
            } else {
                peer
            }
        }
        savePeers(peers)
    }

    fun removeTrust(identityPublicKeyB64: String) {
        val peers = listPeers().filter { it.identityPublicKeyB64 != identityPublicKeyB64 }
        savePeers(peers)
    }

    fun listPeers(): List<TrustedPeer> {
        val json = prefs.getString(KEY_TRUSTED_PEERS, null) ?: return emptyList()
        return try {
            val arr = org.json.JSONArray(json)
            (0 until arr.length()).map { i ->
                val obj = arr.getJSONObject(i)
                TrustedPeer(
                    deviceId = obj.getString("deviceId"),
                    deviceName = obj.getString("deviceName"),
                    identityPublicKeyB64 = obj.getString("identityPublicKeyB64"),
                    pairedAt = obj.getLong("pairedAt"),
                    lastSeen = obj.getLong("lastSeen"),
                )
            }
        } catch (e: Exception) {
            Log.w(TAG, "Failed to parse trust store: ${e.message}")
            emptyList()
        }
    }

    // ------------------------------------------------------------------
    // Private helpers
    // ------------------------------------------------------------------

    private fun savePeers(peers: List<TrustedPeer>) {
        val arr = org.json.JSONArray()
        peers.forEach { peer ->
            arr.put(JSONObject().apply {
                put("deviceId", peer.deviceId)
                put("deviceName", peer.deviceName)
                put("identityPublicKeyB64", peer.identityPublicKeyB64)
                put("pairedAt", peer.pairedAt)
                put("lastSeen", peer.lastSeen)
            })
        }
        prefs.edit().putString(KEY_TRUSTED_PEERS, arr.toString()).apply()
    }
}
