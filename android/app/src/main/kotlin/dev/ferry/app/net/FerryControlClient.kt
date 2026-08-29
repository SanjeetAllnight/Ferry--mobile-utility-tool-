package dev.ferry.app.net

import android.util.Base64
import android.util.Log
import dev.ferry.app.discovery.DiscoveredDevice
import dev.ferry.app.protocol.ProtocolConstants
import dev.ferry.app.security.FerryIdentity
import dev.ferry.app.security.FerrySession
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.InputStream
import java.io.OutputStream
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID

/**
 * FerryControlClient manages the outbound secure control-plane connection from Android
 * to a discovered Linux (or other Android) Ferry peer.
 *
 * Lifecycle:
 *   connect(device) → CONNECTING → HANDSHAKING → PAIRING/AUTHENTICATING → ESTABLISHED
 *   disconnect()    → CLOSING → DISCONNECTED
 *
 * All network I/O runs on Dispatchers.IO.
 * State changes are exposed as a StateFlow for Compose UI observation.
 *
 * Phase 2B implements full handshake with auto-accept pairing (SAS displayed in logs).
 * Phase 2C will add the GNOME-style pairing confirmation dialog.
 */
class FerryControlClient(
    private val identity: FerryIdentity,
    private val trustStore: FerryTrustStore,
) {

    companion object {
        private const val TAG = "FerryControlClient"
        private const val MAGIC_HI = 0x46.toByte()  // 'F'
        private const val MAGIC_LO = 0x59.toByte()  // 'Y'
        private const val MAX_FRAME = 1048576         // 1 MiB
        private const val HANDSHAKE_TIMEOUT_MS = 30_000L
        private const val AUTH_TIMEOUT_MS = 30_000L
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val _sessionState = MutableStateFlow(FerrySession.State.DISCONNECTED)
    val sessionState: StateFlow<FerrySession.State> = _sessionState.asStateFlow()

    private val _connectedDevice = MutableStateFlow<DiscoveredDevice?>(null)
    val connectedDevice: StateFlow<DiscoveredDevice?> = _connectedDevice.asStateFlow()

    private val _sasCode = MutableStateFlow<String?>(null)
    val sasCode: StateFlow<String?> = _sasCode.asStateFlow()

    private var socket: Socket? = null
    private var sessionJob: Job? = null
    private var session: FerrySession? = null

    // ------------------------------------------------------------------
    // Public API
    // ------------------------------------------------------------------

    fun connect(device: DiscoveredDevice) {
        if (_sessionState.value != FerrySession.State.DISCONNECTED) {
            Log.w(TAG, "Already connected or connecting; ignoring connect request")
            return
        }

        Log.i(TAG, "Initiating connection to ${device.deviceName} at ${device.host}:${device.port}")
        _connectedDevice.value = device
        sessionJob = scope.launch { runSession(device) }
    }

    fun disconnect() {
        Log.i(TAG, "Disconnect requested")
        sessionJob?.cancel()
        closeSocket()
    }

    // ------------------------------------------------------------------
    // Session coroutine
    // ------------------------------------------------------------------

    private suspend fun runSession(device: DiscoveredDevice) = withContext(Dispatchers.IO) {
        val sess = FerrySession(isInitiator = true)
        session = sess

        try {
            sess.transition(FerrySession.State.CONNECTING)
            _sessionState.value = FerrySession.State.CONNECTING

            val sock = Socket(device.host, device.port).also { socket = it }
            sock.soTimeout = 0  // No read timeout; we use coroutine cancellation
            val input = sock.getInputStream()
            val output = sock.getOutputStream()

            sess.transition(FerrySession.State.HANDSHAKING)
            _sessionState.value = FerrySession.State.HANDSHAKING

            // ── Step 1: Send HANDSHAKE_INIT ──────────────────────────────
            val (_, ephPub, nonce) = sess.buildLocalHandshake(identity.publicKeyBytes)
            val initPayload = JSONObject().apply {
                put("device_id", device.deviceId)  // will be updated from prefs
                put("device_name", device.deviceName)
                put("device_type", "mobile")
                put("public_key", identity.publicKeyB64)
                put("ephemeral_key", bytesToB64(ephPub))
                put("nonce", bytesToB64(nonce))
                put("is_paired", false)
            }
            sendPlainFrame(output, ProtocolConstants.MessageTypes.HANDSHAKE_INIT, initPayload)

            // ── Step 2: Receive HANDSHAKE_RESPONSE ───────────────────────
            val remoteEnv = recvPlainFrame(input)
            val remoteType = remoteEnv.getString("type")
            check(remoteType == ProtocolConstants.MessageTypes.HANDSHAKE_RESPONSE) {
                "Expected HANDSHAKE_RESPONSE, got $remoteType"
            }
            val remotePayload = remoteEnv.getJSONObject("payload")
            val remoteStaticB64 = remotePayload.getString("public_key")
            val remoteEphB64 = remotePayload.getString("ephemeral_key")
            val remoteNonceB64 = remotePayload.getString("nonce")

            sess.acceptRemoteHandshake(
                FerryIdentity.decodeB64(remoteStaticB64),
                FerryIdentity.decodeB64(remoteEphB64),
                FerryIdentity.decodeB64(remoteNonceB64),
            )

            // ── Step 3: Derive session keys + SAS ───────────────────────
            val sas = sess.deriveKeys()
            _sasCode.value = sas

            val remoteDeviceId = remotePayload.optString("device_id", "")
            val remoteDeviceName = remotePayload.optString("device_name", "")
            val remoteStaticBytes = FerryIdentity.decodeB64(remoteStaticB64)

            val isTrusted = trustStore.isKnownPeer(remoteStaticB64)
            if (isTrusted) {
                sess.transition(FerrySession.State.AUTHENTICATING)
                _sessionState.value = FerrySession.State.AUTHENTICATING
                Log.i(TAG, "Known peer $remoteDeviceName — authenticating")
            } else {
                sess.transition(FerrySession.State.PAIRING)
                _sessionState.value = FerrySession.State.PAIRING
                Log.i(TAG, "New peer $remoteDeviceName — SAS: $sas (auto-accepting, Phase 2C will show UI)")
                // Phase 2B: auto-accept pairing; persist trust
                trustStore.persistTrust(remoteDeviceId, remoteDeviceName, remoteStaticB64)
                sess.transition(FerrySession.State.AUTHENTICATING)
                _sessionState.value = FerrySession.State.AUTHENTICATING
            }

            // ── Step 4: Send AUTH_CHALLENGE (our signature) ──────────────
            val transcript = sess.buildAuthTranscript()
            val ourSig = identity.sign(transcript)
            val authChallengePayload = JSONObject().apply {
                put("signature", bytesToB64(ourSig))
            }
            sendPlainFrame(output, ProtocolConstants.MessageTypes.AUTH_CHALLENGE, authChallengePayload)

            // ── Step 5: Receive AUTH_RESPONSE ────────────────────────────
            val authResp = recvPlainFrame(input)
            val authRespType = authResp.getString("type")
            check(authRespType == ProtocolConstants.MessageTypes.AUTH_RESPONSE) {
                "Expected AUTH_RESPONSE, got $authRespType"
            }
            val remoteSigB64 = authResp.getJSONObject("payload").getString("signature")
            val remoteSig = FerryIdentity.decodeB64(remoteSigB64)

            check(FerryIdentity.verify(remoteStaticBytes, transcript, remoteSig)) {
                "AUTH signature verification failed — rejecting peer"
            }
            Log.i(TAG, "Auth verified for $remoteDeviceName")

            // ── Step 6: ESTABLISHED ──────────────────────────────────────
            sess.transition(FerrySession.State.ESTABLISHED)
            _sessionState.value = FerrySession.State.ESTABLISHED
            Log.i(TAG, "Session ESTABLISHED with $remoteDeviceName")

            // ── Step 7: Encrypted session loop ───────────────────────────
            runEncryptedSessionLoop(sess, input)

        } catch (e: CancellationException) {
            Log.i(TAG, "Session cancelled")
        } catch (e: Exception) {
            Log.e(TAG, "Session failed", e)
        } finally {
            _sasCode.value = null
            closeSocket()
            val finalState = when (session?.state) {
                FerrySession.State.ESTABLISHED -> {
                    try { session?.transition(FerrySession.State.CLOSING) } catch (_: Exception) {}
                    try { session?.transition(FerrySession.State.DISCONNECTED) } catch (_: Exception) {}
                    FerrySession.State.DISCONNECTED
                }
                FerrySession.State.FAILED -> FerrySession.State.FAILED
                else -> FerrySession.State.DISCONNECTED
            }
            _sessionState.value = finalState
            _connectedDevice.value = null
        }
    }

    // ------------------------------------------------------------------
    // Encrypted session loop
    // ------------------------------------------------------------------

    private fun runEncryptedSessionLoop(sess: FerrySession, input: InputStream) {
        try {
            while (true) {
                val frameBytes = readAeadFrameBytes(input) ?: break
                val plaintext = sess.decryptFrame(frameBytes)
                val envelope = JSONObject(String(plaintext, Charsets.UTF_8))
                val type = envelope.getString("type")

                if (type == ProtocolConstants.MessageTypes.DISCONNECT) {
                    Log.i(TAG, "Peer sent DISCONNECT")
                    break
                }
                Log.d(TAG, "Received encrypted message: type=$type")
                // Future: route to transfer handler
            }
        } catch (e: Exception) {
            Log.e(TAG, "Encrypted session loop ended", e)
        } finally {
            try { sess.transition(FerrySession.State.CLOSING) } catch (_: Exception) {}
            try { sess.transition(FerrySession.State.DISCONNECTED) } catch (_: Exception) {}
        }
    }

    // ------------------------------------------------------------------
    // Plain frame I/O (handshake phase)
    // ------------------------------------------------------------------

    private fun sendPlainFrame(output: OutputStream, type: String, payload: JSONObject) {
        val envelope = JSONObject().apply {
            put("protocol_version", ProtocolConstants.PROTOCOL_VERSION)
            put("message_id", UUID.randomUUID().toString())
            put("reply_to", JSONObject.NULL)
            put("timestamp", System.currentTimeMillis())
            put("type", type)
            put("payload", payload)
        }
        val payloadBytes = envelope.toString().toByteArray(Charsets.UTF_8)
        check(payloadBytes.size <= MAX_FRAME) { "Frame too large: ${payloadBytes.size}" }

        val frame = ByteBuffer.allocate(6 + payloadBytes.size)
            .order(ByteOrder.BIG_ENDIAN)
            .put(MAGIC_HI).put(MAGIC_LO)
            .putInt(payloadBytes.size)
            .put(payloadBytes)
            .array()

        output.write(frame)
        output.flush()
    }

    private fun recvPlainFrame(input: InputStream): JSONObject {
        val header = input.readNBytes(6)
        check(header.size == 6) { "Connection closed during header read" }
        check(header[0] == MAGIC_HI && header[1] == MAGIC_LO) {
            "Invalid magic bytes: ${header[0].toUByte()}, ${header[1].toUByte()}"
        }
        val length = ByteBuffer.wrap(header, 2, 4).order(ByteOrder.BIG_ENDIAN).int
        check(length in 1..MAX_FRAME) { "Invalid frame length: $length" }

        val payload = input.readNBytes(length)
        check(payload.size == length) { "Connection closed during payload read" }
        return JSONObject(String(payload, Charsets.UTF_8))
    }

    // ------------------------------------------------------------------
    // AEAD frame I/O (post-handshake)
    // ------------------------------------------------------------------

    private fun readAeadFrameBytes(input: InputStream): ByteArray? {
        val header = input.readNBytes(4)
        if (header.isEmpty()) return null
        check(header.size == 4) { "Connection closed during AEAD header read" }

        val ctLen = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN).int
        check(ctLen in 0..MAX_FRAME) { "AEAD frame ciphertext length out of range: $ctLen" }

        val rest = input.readNBytes(12 + ctLen)  // 12-byte nonce + ciphertext
        check(rest.size == 12 + ctLen) { "Connection closed during AEAD payload read" }

        return header + rest
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    private fun bytesToB64(bytes: ByteArray): String =
        Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)

    private fun closeSocket() {
        try { socket?.close() } catch (_: Exception) {}
        socket = null
    }
}
