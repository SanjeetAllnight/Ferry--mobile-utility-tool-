package dev.ferry.app.security

import android.util.Base64
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.SecretKey
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * FerrySession manages one secure session between two Ferry peers on Android.
 *
 * Implements the Phase 2B handshake:
 *  1. X25519 ephemeral key exchange (javax.crypto.KeyAgreement)
 *  2. HKDF-SHA256 session key derivation
 *  3. SAS code derivation (6-digit PIN for out-of-band verification)
 *  4. ChaCha20-Poly1305 AEAD framing (Android API 28+, SDK 36 on test device)
 *  5. Ed25519 signature-based mutual authentication
 *
 * CRITICAL: No private key material is ever logged or transmitted.
 *
 * Note: ChaCha20-Poly1305 in Android uses "ChaCha20-Poly1305" Cipher instance (API 28+).
 * Nonce format: 4 zero bytes + 8-byte big-endian counter (matches Linux implementation).
 */
class FerrySession(private val isInitiator: Boolean) {

    companion object {
        private const val TAG = "FerrySession"

        // Key derivation info constants — must match Linux session.py exactly
        private val SESSION_KEY_INFO_INITIATOR = "ferry-session-initiator-v1".toByteArray()
        private val SESSION_KEY_INFO_RESPONDER = "ferry-session-responder-v1".toByteArray()
        private val SAS_HKDF_INFO = "ferry-sas-v1".toByteArray()
        private const val SESSION_KEY_LEN = 32
        private const val SAS_BYTES = 3
        private const val AEAD_NONCE_SIZE = 12
        private const val AEAD_HEADER_SIZE = 4  // 32-bit big-endian ciphertext length
        private const val MAX_AEAD_PLAINTEXT = 1048576  // 1 MiB
    }

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------

    enum class State {
        DISCONNECTED, CONNECTING, HANDSHAKING, PAIRING,
        WAITING_FOR_LOCAL_DECISION, WAITING_FOR_REMOTE_DECISION, PAIR_ACCEPTED,
        AUTHENTICATING, ESTABLISHED, CLOSING, FAILED
    }

    var state: State = State.DISCONNECTED
        private set

    // Ephemeral X25519 keypair
    private var ephemeralKeyAgreement: KeyAgreement? = null
    private var ephemeralPublicKeyBytes: ByteArray? = null

    // Handshake data
    private var localStaticPub: ByteArray? = null
    private var localEphPub: ByteArray? = null
    private var localNonce: ByteArray? = null

    private var remoteStaticPub: ByteArray? = null
    private var remoteEphPub: ByteArray? = null
    private var remoteNonce: ByteArray? = null

    // Derived AEAD keys
    private var sendKey: ByteArray? = null
    private var recvKey: ByteArray? = null
    private var sasBytes: ByteArray? = null

    // Nonce counters
    private var sendCounter: Long = 0L
    private var recvCounter: Long = 0L

    // ------------------------------------------------------------------
    // SAS
    // ------------------------------------------------------------------

    /**
     * 6-digit SAS verification code (available after deriveKeys()).
     */
    val sasCode: String?
        get() {
            val sb = sasBytes ?: return null
            val value = ((sb[0].toInt() and 0xFF) shl 16) or
                    ((sb[1].toInt() and 0xFF) shl 8) or
                    (sb[2].toInt() and 0xFF)
            return (value % 1_000_000).toString().padStart(6, '0')
        }

    // ------------------------------------------------------------------
    // Ephemeral X25519 keypair
    // ------------------------------------------------------------------

    fun generateEphemeralKeypair(): ByteArray {
        val kpg = java.security.KeyPairGenerator.getInstance("X25519")
        val kp = kpg.generateKeyPair()

        val ka = KeyAgreement.getInstance("X25519")
        ka.init(kp.private)
        ephemeralKeyAgreement = ka

        // Extract raw 32-byte X25519 public key
        // X.509 encoding for X25519: 12-byte header + 32-byte key
        val encoded = kp.public.encoded
        ephemeralPublicKeyBytes = encoded.takeLast(32).toByteArray()
        return ephemeralPublicKeyBytes!!
    }

    // ------------------------------------------------------------------
    // Handshake data building
    // ------------------------------------------------------------------

    fun buildLocalHandshake(staticPublicKey: ByteArray): Triple<ByteArray, ByteArray, ByteArray> {
        val ephPub = ephemeralPublicKeyBytes ?: generateEphemeralKeypair()
        val nonce = ByteArray(16).also { java.security.SecureRandom().nextBytes(it) }

        localStaticPub = staticPublicKey
        localEphPub = ephPub
        localNonce = nonce

        return Triple(staticPublicKey, ephPub, nonce)
    }

    fun acceptRemoteHandshake(staticPub: ByteArray, ephPub: ByteArray, nonce: ByteArray) {
        remoteStaticPub = staticPub
        remoteEphPub = ephPub
        remoteNonce = nonce
    }

    // ------------------------------------------------------------------
    // Key derivation (X25519 DH + HKDF)
    // ------------------------------------------------------------------

    fun deriveKeys(): String? {
        val ka = ephemeralKeyAgreement ?: error("Ephemeral key agreement not initialized")
        val remoteEph = remoteEphPub ?: error("Remote ephemeral key not set")

        // Reconstruct remote X25519 public key from raw bytes
        val x509Header = byteArrayOf(
            0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65,
            0x6e, 0x03, 0x21, 0x00
        )
        val remoteX509 = x509Header + remoteEph
        val kf = java.security.KeyFactory.getInstance("X25519")
        val remotePublicKey = kf.generatePublic(java.security.spec.X509EncodedKeySpec(remoteX509))

        ka.doPhase(remotePublicKey, true)
        val dhShared: ByteArray = ka.generateSecret()

        // Discard ephemeral key after use
        ephemeralKeyAgreement = null

        // Determine initiator/responder ordering (same as Linux)
        val initStaticPub: ByteArray
        val respStaticPub: ByteArray
        val initEphPub: ByteArray
        val respEphPub: ByteArray
        val initNonce: ByteArray
        val respNonce: ByteArray

        if (isInitiator) {
            initStaticPub = localStaticPub!!
            respStaticPub = remoteStaticPub!!
            initEphPub = localEphPub!!
            respEphPub = remoteEphPub!!
            initNonce = localNonce!!
            respNonce = remoteNonce!!
        } else {
            initStaticPub = remoteStaticPub!!
            respStaticPub = localStaticPub!!
            initEphPub = remoteEphPub!!
            respEphPub = localEphPub!!
            initNonce = remoteNonce!!
            respNonce = localNonce!!
        }

        val transcriptSalt = initStaticPub + respStaticPub + initEphPub + respEphPub + initNonce + respNonce

        val initKey = hkdfSha256(dhShared, transcriptSalt, SESSION_KEY_INFO_INITIATOR, SESSION_KEY_LEN)
        val respKey = hkdfSha256(dhShared, transcriptSalt, SESSION_KEY_INFO_RESPONDER, SESSION_KEY_LEN)
        val sas = hkdfSha256(dhShared, transcriptSalt, SAS_HKDF_INFO, SAS_BYTES)

        sasBytes = sas
        if (isInitiator) {
            sendKey = initKey
            recvKey = respKey
        } else {
            sendKey = respKey
            recvKey = initKey
        }

        Log.d(TAG, "Session keys derived. SAS: $sasCode")
        return sasCode
    }

    // ------------------------------------------------------------------
    // Auth transcript
    // ------------------------------------------------------------------

    fun buildAuthTranscript(): ByteArray {
        val prefix = "ferry-auth-v1|".toByteArray()
        return if (isInitiator) {
            prefix + localStaticPub!! + localEphPub!! + localNonce!! +
                    remoteStaticPub!! + remoteEphPub!! + remoteNonce!!
        } else {
            prefix + remoteStaticPub!! + remoteEphPub!! + remoteNonce!! +
                    localStaticPub!! + localEphPub!! + localNonce!!
        }
    }

    // ------------------------------------------------------------------
    // AEAD framing: ChaCha20-Poly1305
    // ------------------------------------------------------------------

    /**
     * Encrypt plaintext into an AEAD frame.
     * Frame: [4-byte big-endian ciphertext_len][12-byte nonce][ciphertext+tag]
     */
    fun encryptFrame(plaintext: ByteArray): ByteArray {
        require(plaintext.size <= MAX_AEAD_PLAINTEXT) { "Plaintext too large" }
        val key = sendKey ?: error("Session keys not derived")

        val nonce = makeNonce(sendCounter)
        sendCounter++

        val cipher = Cipher.getInstance("ChaCha20-Poly1305")
        val keySpec = SecretKeySpec(key, "ChaCha20")
        cipher.init(Cipher.ENCRYPT_MODE, keySpec, IvParameterSpec(nonce))
        val ciphertext = cipher.doFinal(plaintext)

        return ByteBuffer.allocate(AEAD_HEADER_SIZE + AEAD_NONCE_SIZE + ciphertext.size)
            .order(ByteOrder.BIG_ENDIAN)
            .putInt(ciphertext.size)
            .put(nonce)
            .put(ciphertext)
            .array()
    }

    /**
     * Decrypt an AEAD frame received from peer.
     * Verifies nonce counter for replay protection.
     */
    fun decryptFrame(frame: ByteArray): ByteArray {
        require(frame.size >= AEAD_HEADER_SIZE + AEAD_NONCE_SIZE) { "Frame too short" }
        val key = recvKey ?: error("Session keys not derived")

        val ctLen = ByteBuffer.wrap(frame, 0, 4).order(ByteOrder.BIG_ENDIAN).int
        val expectedTotal = AEAD_HEADER_SIZE + AEAD_NONCE_SIZE + ctLen
        require(frame.size >= expectedTotal) { "Frame truncated" }

        val frameNonce = frame.copyOfRange(AEAD_HEADER_SIZE, AEAD_HEADER_SIZE + AEAD_NONCE_SIZE)
        val expectedNonce = makeNonce(recvCounter)
        require(frameNonce.contentEquals(expectedNonce)) {
            "Nonce mismatch: replay or reorder detected (counter=$recvCounter)"
        }
        recvCounter++

        val ciphertext = frame.copyOfRange(AEAD_HEADER_SIZE + AEAD_NONCE_SIZE, expectedTotal)

        val cipher = Cipher.getInstance("ChaCha20-Poly1305")
        val keySpec = SecretKeySpec(key, "ChaCha20")
        cipher.init(Cipher.DECRYPT_MODE, keySpec, IvParameterSpec(frameNonce))
        return cipher.doFinal(ciphertext)
    }

    // ------------------------------------------------------------------
    // State machine
    // ------------------------------------------------------------------

    fun transition(newState: State) {
        val valid = mapOf(
            State.DISCONNECTED to setOf(State.CONNECTING),
            State.CONNECTING to setOf(State.HANDSHAKING, State.FAILED),
            State.HANDSHAKING to setOf(State.AUTHENTICATING, State.FAILED),
            State.AUTHENTICATING to setOf(State.ESTABLISHED, State.PAIRING, State.FAILED),
            State.PAIRING to setOf(State.WAITING_FOR_LOCAL_DECISION, State.WAITING_FOR_REMOTE_DECISION, State.FAILED),
            State.WAITING_FOR_LOCAL_DECISION to setOf(State.WAITING_FOR_REMOTE_DECISION, State.PAIR_ACCEPTED, State.FAILED),
            State.WAITING_FOR_REMOTE_DECISION to setOf(State.WAITING_FOR_LOCAL_DECISION, State.PAIR_ACCEPTED, State.FAILED),
            State.PAIR_ACCEPTED to setOf(State.ESTABLISHED, State.FAILED),
            State.ESTABLISHED to setOf(State.CLOSING, State.FAILED),
            State.CLOSING to setOf(State.DISCONNECTED),
            State.FAILED to setOf(State.DISCONNECTED),
        )
        val allowed = valid[state] ?: emptySet()
        require(newState in allowed) {
            "Invalid session transition: $state → $newState"
        }
        Log.d(TAG, "Session state: $state → $newState")
        state = newState
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun makeNonce(counter: Long): ByteArray {
        return ByteBuffer.allocate(AEAD_NONCE_SIZE)
            .order(ByteOrder.BIG_ENDIAN)
            .putInt(0)        // 4 zero bytes
            .putLong(counter) // 8-byte counter
            .array()
    }

    /**
     * HKDF-SHA256 implementation using Android's javax.crypto.Mac (HMAC-SHA256).
     * Implements RFC 5869.
     */
    private fun hkdfSha256(
        inputKeyMaterial: ByteArray,
        salt: ByteArray,
        info: ByteArray,
        length: Int,
    ): ByteArray {
        val hmacAlgo = "HmacSHA256"
        val hashLen = 32

        // Extract phase
        val mac = javax.crypto.Mac.getInstance(hmacAlgo)
        mac.init(SecretKeySpec(salt, hmacAlgo))
        val prk = mac.doFinal(inputKeyMaterial)

        // Expand phase
        val result = ByteArray(length)
        var t = ByteArray(0)
        var offset = 0
        var counter = 1

        while (offset < length) {
            mac.init(SecretKeySpec(prk, hmacAlgo))
            mac.update(t)
            mac.update(info)
            mac.update(counter.toByte())
            t = mac.doFinal()

            val toCopy = minOf(hashLen, length - offset)
            t.copyInto(result, offset, 0, toCopy)
            offset += toCopy
            counter++
        }
        return result
    }
}
