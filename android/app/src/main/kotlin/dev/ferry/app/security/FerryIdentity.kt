package dev.ferry.app.security

import android.content.Context
import android.util.Base64
import android.util.Log
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec

/**
 * FerryIdentity manages the device's long-term Ed25519 cryptographic identity keypair.
 *
 * The private key is stored in SharedPreferences under private mode (Context.MODE_PRIVATE).
 * Phase 2C will migrate to Android Keystore for hardware-backed protection.
 *
 * IMPORTANT: Private key material is NEVER logged or transmitted.
 * The public key (32-byte Ed25519) is safe to share and is transmitted during handshake.
 *
 * Note: Ed25519 requires Android API 33+ (Android 13) for native support.
 * The physical test device (Realme RMX3870) runs Android 16/SDK 36 — fully supported.
 */
class FerryIdentity(context: Context) {

    companion object {
        private const val TAG = "FerryIdentity"
        private const val PREFS_NAME = "ferry_identity"
        private const val KEY_PRIVATE = "identity_private_key_pkcs8_b64"
        private const val KEY_PUBLIC = "identity_public_key_x509_b64"
        private const val ALGORITHM = "Ed25519"
        private const val SIGNATURE_ALGORITHM = "Ed25519"

        /**
         * Verify an Ed25519 signature from a remote peer.
         *
         * @param publicKeyBytes Raw 32-byte public key of the signer.
         * @param message        Message that was signed.
         * @param signature      64-byte raw Ed25519 signature.
         * @return true if valid, false if verification fails.
         */
        fun verify(publicKeyBytes: ByteArray, message: ByteArray, signature: ByteArray): Boolean {
            return try {
                // Reconstruct the X.509 SubjectPublicKeyInfo header for Ed25519:
                // 30 2a 30 05 06 03 2b 65 70 03 21 00 <32-byte key>
                val header = byteArrayOf(
                    0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65,
                    0x70, 0x03, 0x21, 0x00
                )
                val x509Bytes = header + publicKeyBytes
                val kf = KeyFactory.getInstance("Ed25519")
                val publicKey = kf.generatePublic(X509EncodedKeySpec(x509Bytes))
                val verifier = Signature.getInstance("Ed25519")
                verifier.initVerify(publicKey)
                verifier.update(message)
                verifier.verify(signature)
            } catch (e: Exception) {
                Log.w(TAG, "Signature verification failed: ${e.message}")
                false
            }
        }

        /**
         * Decode a URL-safe base64 (no padding) string to raw bytes.
         */
        fun decodeB64(b64: String): ByteArray {
            val padded = when (b64.length % 4) {
                2 -> "$b64=="
                3 -> "$b64="
                else -> b64
            }
            return Base64.decode(padded, Base64.URL_SAFE)
        }
    }

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
    private val keyPair: KeyPair = loadOrGenerateKeyPair()

    /**
     * The raw 32-byte Ed25519 public key bytes.
     * Safe to transmit over the network.
     */
    val publicKeyBytes: ByteArray
        get() {
            // Android's X.509 encoding for Ed25519 has a 12-byte header;
            // the raw key is the last 32 bytes.
            val encoded = keyPair.public.encoded
            return encoded.takeLast(32).toByteArray()
        }

    /**
     * URL-safe Base64 encoded public key (no padding).
     * Used in HANDSHAKE_INIT / HANDSHAKE_RESPONSE payloads.
     */
    val publicKeyB64: String
        get() = Base64.encodeToString(publicKeyBytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)

    /**
     * Sign arbitrary bytes with the local Ed25519 private key.
     * Returns 64-byte raw signature.
     * The private key NEVER leaves this function.
     */
    fun sign(message: ByteArray): ByteArray {
        val signer = Signature.getInstance(SIGNATURE_ALGORITHM)
        signer.initSign(keyPair.private)
        signer.update(message)
        return signer.sign()
    }

    // ------------------------------------------------------------------
    // Key persistence
    // ------------------------------------------------------------------

    private fun loadOrGenerateKeyPair(): KeyPair {
        val privateB64 = prefs.getString(KEY_PRIVATE, null)
        val publicB64 = prefs.getString(KEY_PUBLIC, null)

        if (privateB64 != null && publicB64 != null) {
            try {
                return loadKeyPair(privateB64, publicB64)
            } catch (e: Exception) {
                Log.w(TAG, "Failed to load identity keypair, generating new one: ${e.message}")
            }
        }

        return generateAndSaveKeyPair()
    }

    private fun loadKeyPair(privateB64: String, publicB64: String): KeyPair {
        val kf = KeyFactory.getInstance(ALGORITHM)
        val privateBytes = Base64.decode(privateB64, Base64.DEFAULT)
        val publicBytes = Base64.decode(publicB64, Base64.DEFAULT)
        val privateKey: PrivateKey = kf.generatePrivate(PKCS8EncodedKeySpec(privateBytes))
        val publicKey: PublicKey = kf.generatePublic(X509EncodedKeySpec(publicBytes))
        return KeyPair(publicKey, privateKey)
    }

    private fun generateAndSaveKeyPair(): KeyPair {
        val kpg = KeyPairGenerator.getInstance(ALGORITHM)
        kpg.initialize(255) // Ed25519 uses 255-bit key
        val kp = kpg.generateKeyPair()

        // Store as Base64 encoded DER — private key NEVER appears in logs
        val privateB64 = Base64.encodeToString(kp.private.encoded, Base64.DEFAULT)
        val publicB64 = Base64.encodeToString(kp.public.encoded, Base64.DEFAULT)

        prefs.edit()
            .putString(KEY_PRIVATE, privateB64)
            .putString(KEY_PUBLIC, publicB64)
            .apply()

        Log.i(TAG, "Generated new Ed25519 identity. Public key: ${publicKeyBytesToB64(kp.public)}...")
        return kp
    }

    private fun publicKeyBytesToB64(pub: PublicKey): String {
        val raw = pub.encoded.takeLast(32).toByteArray()
        return Base64.encodeToString(raw, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING).take(12)
    }
}
