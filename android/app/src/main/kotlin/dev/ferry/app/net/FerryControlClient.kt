package dev.ferry.app.net

import android.util.Base64
import android.util.Log
import dev.ferry.app.discovery.DiscoveredDevice
import dev.ferry.app.protocol.ProtocolConstants
import dev.ferry.app.security.FerryIdentity
import dev.ferry.app.security.FerrySession
import dev.ferry.app.transfer.FerryTransferReceiver
import dev.ferry.app.transfer.TransferMetadata
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
import java.io.File
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
    val sasCode: StateFlow<String?> = _sasCode

    private var pendingPeerId: String? = null
    private var pendingPeerName: String? = null
    private var pendingPeerStaticB64: String? = null
    private var activeOutput: OutputStream? = null

    private var socket: Socket? = null
    private var sessionJob: Job? = null
    private var session: FerrySession? = null

    // Active incoming transfer (Phase 3A MVP: one at a time)
    private var activeReceiver: FerryTransferReceiver? = null
    private val stagingDir: File get() = File(
        android.os.Environment.getExternalStoragePublicDirectory(
            android.os.Environment.DIRECTORY_DOWNLOADS
        ),
        "Ferry/staging"
    )

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

            // Transition to AUTHENTICATING to exchange signatures first
            sess.transition(FerrySession.State.AUTHENTICATING)
            _sessionState.value = FerrySession.State.AUTHENTICATING

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

            val isTrusted = trustStore.isKnownPeer(remoteStaticB64)
            if (isTrusted) {
                sess.transition(FerrySession.State.ESTABLISHED)
                _sessionState.value = FerrySession.State.ESTABLISHED
                Log.i(TAG, "Session ESTABLISHED with $remoteDeviceName")
                runEncryptedSessionLoop(sess, input, output)
            } else {
                sess.transition(FerrySession.State.PAIRING)
                _sessionState.value = FerrySession.State.PAIRING
                Log.i(TAG, "New peer $remoteDeviceName — Requesting pairing, SAS: $sas")
                
                // Store peer data for persistence after accept
                this@FerryControlClient.pendingPeerId = remoteDeviceId
                this@FerryControlClient.pendingPeerName = remoteDeviceName
                this@FerryControlClient.pendingPeerStaticB64 = remoteStaticB64
                
                runEncryptedSessionLoop(sess, input, output)
            }

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
            activeOutput = null
        }
    }

    fun acceptPairing() {
        val sess = session ?: return
        if (sess.state != FerrySession.State.PAIRING && sess.state != FerrySession.State.WAITING_FOR_LOCAL_DECISION) return
        Log.i(TAG, "User accepted pairing")
        
        if (sess.state == FerrySession.State.PAIRING) {
            sess.transition(FerrySession.State.WAITING_FOR_REMOTE_DECISION)
            _sessionState.value = FerrySession.State.WAITING_FOR_REMOTE_DECISION
        } else if (sess.state == FerrySession.State.WAITING_FOR_LOCAL_DECISION) {
            sess.transition(FerrySession.State.PAIR_ACCEPTED)
            persistPendingTrust()
            sess.transition(FerrySession.State.ESTABLISHED)
            _sessionState.value = FerrySession.State.ESTABLISHED
        }
        
        scope.launch {
            try {
                val payload = JSONObject().apply { put("decision", "ACCEPT") }
                sendEncrypted(sess, ProtocolConstants.MessageTypes.PAIR_DECISION, payload)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to send pairing acceptance", e)
            }
        }
    }

    fun rejectPairing() {
        val sess = session ?: return
        Log.i(TAG, "User rejected pairing")
        scope.launch {
            try {
                val payload = JSONObject().apply { put("decision", "REJECT") }
                sendEncrypted(sess, ProtocolConstants.MessageTypes.PAIR_DECISION, payload)
            } catch (e: Exception) {
                Log.e(TAG, "Failed to send pairing rejection", e)
            }
        }
        
        try { sess.transition(FerrySession.State.FAILED) } catch (_: Exception) {}
        _sessionState.value = FerrySession.State.FAILED
        closeSocket()
    }

    private fun persistPendingTrust() {
        if (pendingPeerId != null && pendingPeerName != null && pendingPeerStaticB64 != null) {
            trustStore.persistTrust(pendingPeerId!!, pendingPeerName!!, pendingPeerStaticB64!!)
            Log.i(TAG, "Trust persisted for $pendingPeerName")
        }
    }

    private fun sendEncrypted(sess: FerrySession, type: String, payload: JSONObject) {
        val out = activeOutput ?: return
        val envelope = JSONObject().apply {
            put("protocol_version", ProtocolConstants.PROTOCOL_VERSION)
            put("message_id", UUID.randomUUID().toString())
            put("reply_to", JSONObject.NULL)
            put("type", type)
            put("payload", payload)
        }
        val plaintext = envelope.toString().toByteArray(Charsets.UTF_8)
        val frame = sess.encryptFrame(plaintext)
        out.write(frame)
        out.flush()
    }

    // ------------------------------------------------------------------
    // Encrypted session loop
    // ------------------------------------------------------------------

    private fun runEncryptedSessionLoop(sess: FerrySession, input: InputStream, output: OutputStream) {
        this.activeOutput = output
        try {
            while (true) {
                val frameBytes = readAeadFrameBytes(input) ?: break
                val plaintext = sess.decryptFrame(frameBytes)

                // Binary TRANSFER_CHUNK frames start with FYCH magic — not JSON
                if (FerryTransferReceiver.isChunkFrame(plaintext)) {
                    onChunkReceived(plaintext)
                    continue
                }

                val envelope = JSONObject(String(plaintext, Charsets.UTF_8))
                val type = envelope.getString("type")

                if (type == ProtocolConstants.MessageTypes.DISCONNECT) {
                    Log.i(TAG, "Peer sent DISCONNECT")
                    break
                }
                
                if (type == ProtocolConstants.MessageTypes.PAIR_DECISION) {
                    val decision = envelope.getJSONObject("payload").optString("decision")
                    Log.i(TAG, "Received PAIR_DECISION: $decision")
                    if (decision == "REJECT") {
                        Log.w(TAG, "Peer rejected pairing")
                        break
                    } else if (decision == "ACCEPT") {
                        if (sess.state == FerrySession.State.PAIRING) {
                            sess.transition(FerrySession.State.WAITING_FOR_LOCAL_DECISION)
                            _sessionState.value = FerrySession.State.WAITING_FOR_LOCAL_DECISION
                        } else if (sess.state == FerrySession.State.WAITING_FOR_REMOTE_DECISION) {
                            sess.transition(FerrySession.State.PAIR_ACCEPTED)
                            persistPendingTrust()
                            sess.transition(FerrySession.State.ESTABLISHED)
                            _sessionState.value = FerrySession.State.ESTABLISHED
                        }
                    }
                    continue
                }

                Log.d(TAG, "Received encrypted message: type=$type")
                handleTransferMessage(sess, output, type, envelope.getJSONObject("payload"))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Encrypted session loop ended", e)
        } finally {
            try { sess.transition(FerrySession.State.CLOSING) } catch (_: Exception) {}
            try { sess.transition(FerrySession.State.DISCONNECTED) } catch (_: Exception) {}
        }
    }

    // ------------------------------------------------------------------
    // Transfer message routing (Phase 3A)
    // ------------------------------------------------------------------

    private fun onChunkReceived(plaintext: ByteArray) {
        try {
            val (transferId, seq, data) = FerryTransferReceiver.decodeChunkFrame(plaintext)
            val receiver = activeReceiver
            if (receiver == null) {
                Log.w(TAG, "TRANSFER_CHUNK for unknown transfer_id ${transferId.take(8)} (seq=$seq)")
                return
            }
            receiver.receiveChunk(transferId, seq, data)
        } catch (e: Exception) {
            Log.e(TAG, "Error processing TRANSFER_CHUNK", e)
            activeReceiver?.cancel()
            activeReceiver = null
        }
    }

    private fun handleTransferMessage(
        sess: FerrySession,
        output: OutputStream,
        type: String,
        payload: JSONObject,
    ) {
        val transferId = payload.optString("transfer_id", "")
        when (type) {
            ProtocolConstants.MessageTypes.TRANSFER_REQUEST -> {
                onTransferRequest(sess, output, payload)
            }
            ProtocolConstants.MessageTypes.TRANSFER_COMPLETE -> {
                onTransferComplete(sess, output, transferId)
            }
            ProtocolConstants.MessageTypes.TRANSFER_CANCEL -> {
                Log.i(TAG, "TRANSFER_CANCEL received for ${transferId.take(8)}")
                activeReceiver?.cancel()
                activeReceiver = null
            }
            ProtocolConstants.MessageTypes.TRANSFER_ERROR -> {
                Log.e(TAG, "TRANSFER_ERROR from peer: [${payload.optString("error_code")}] ${payload.optString("message")}")
                activeReceiver?.cancel()
                activeReceiver = null
            }
            else -> Log.w(TAG, "Unhandled transfer message type: $type")
        }
    }

    private fun onTransferRequest(sess: FerrySession, output: OutputStream, payload: JSONObject) {
        // Phase 3A: auto-accept for trusted peers (UI integration in Phase 3B)
        val transferId = payload.optString("transfer_id", "")
        if (activeReceiver != null) {
            Log.w(TAG, "Busy — rejecting transfer ${transferId.take(8)}")
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_REJECT,
                org.json.JSONObject().apply {
                    put("transfer_id", transferId)
                    put("reason", "BUSY")
                })
            return
        }
        try {
            val meta = TransferMetadata.fromJson(payload)
            stagingDir.mkdirs()
            val receiver = FerryTransferReceiver(meta, stagingDir)
            activeReceiver = receiver
            receiver.begin()
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_ACCEPT,
                org.json.JSONObject().apply { put("transfer_id", meta.transferId) })
            Log.i(TAG, "TRANSFER_ACCEPT sent for ${meta.fileName}")
        } catch (e: Exception) {
            Log.e(TAG, "Invalid TRANSFER_REQUEST: ${e.message}")
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_ERROR,
                org.json.JSONObject().apply {
                    put("transfer_id", transferId)
                    put("error_code", "INVALID_REQUEST")
                    put("message", e.message ?: "")
                })
        }
    }

    private fun onTransferComplete(sess: FerrySession, output: OutputStream, transferId: String) {
        val receiver = activeReceiver
        if (receiver == null) {
            Log.w(TAG, "TRANSFER_COMPLETE for unknown transfer_id ${transferId.take(8)}")
            return
        }
        val success = receiver.finalise()
        activeReceiver = null
        sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_RESULT,
            org.json.JSONObject().apply {
                put("transfer_id", transferId)
                put("success", success)
                put("sha256", if (success) receiver.let { "" } else "")
            })
    }

    private fun sendEncryptedMessage(sess: FerrySession, output: OutputStream, type: String, payload: JSONObject) {
        val envelope = JSONObject().apply {
            put("protocol_version", ProtocolConstants.PROTOCOL_VERSION)
            put("message_id", UUID.randomUUID().toString())
            put("reply_to", JSONObject.NULL)
            put("timestamp", System.currentTimeMillis())
            put("type", type)
            put("payload", payload)
        }
        val plaintext = envelope.toString().toByteArray(Charsets.UTF_8)
        val frame = sess.encryptFrame(plaintext)
        output.write(frame)
        output.flush()
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
