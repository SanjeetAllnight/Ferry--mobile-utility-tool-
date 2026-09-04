package dev.ferry.app.net

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import android.util.Log
import dev.ferry.app.discovery.DiscoveredDevice
import dev.ferry.app.protocol.ProtocolConstants
import dev.ferry.app.security.FerryIdentity
import dev.ferry.app.security.FerrySession
import dev.ferry.app.transfer.FerryTransferClient
import dev.ferry.app.transfer.FerryTransferReceiver
import dev.ferry.app.transfer.TransferMetadata
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
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
 * State changes are exposed as StateFlows for Compose UI observation.
 *
 * Phase 3C:
 *  - sendFile(uri, context, ...) replaces sendFile(fileBytes, ...) in production.
 *  - Streaming via FerryTransferClient.streamChunksFromStream (no full-file load).
 *  - transferProgress / incomingTransferProgress StateFlows for Compose progress UI.
 *  - CompletableDeferred replaces the polling loop for TRANSFER_ACCEPT.
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

    // ── Transfer progress data class ─────────────────────────────────────────

    data class TransferProgress(
        val transferId: String,
        val fileName: String,
        val bytesDone: Long,
        val totalBytes: Long,
        val direction: Direction,
    ) {
        enum class Direction { OUTGOING, INCOMING }

        val fraction: Float get() =
            if (totalBytes > 0) (bytesDone.toFloat() / totalBytes).coerceIn(0f, 1f) else (if (bytesDone >= totalBytes) 1f else 0f)
    }

    // ── Transfer history entry ────────────────────────────────────────────────

    data class TransferHistoryEntry(
        val transferId: String,
        val fileName: String,
        val fileSize: Long,
        val direction: TransferProgress.Direction,
        val success: Boolean,
        val timestampMs: Long,
    )

    // ── State Flows ───────────────────────────────────────────────────────────

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private val _sessionState = MutableStateFlow(FerrySession.State.DISCONNECTED)
    val sessionState: StateFlow<FerrySession.State> = _sessionState.asStateFlow()

    private val _connectedDevice = MutableStateFlow<DiscoveredDevice?>(null)
    val connectedDevice: StateFlow<DiscoveredDevice?> = _connectedDevice.asStateFlow()

    private val _sasCode = MutableStateFlow<String?>(null)
    val sasCode: StateFlow<String?> = _sasCode

    private val _transferProgress = MutableStateFlow<TransferProgress?>(null)
    /** Outgoing transfer progress; null when idle. */
    val transferProgress: StateFlow<TransferProgress?> = _transferProgress.asStateFlow()

    private val _incomingProgress = MutableStateFlow<TransferProgress?>(null)
    /** Incoming transfer progress; null when idle. */
    val incomingTransferProgress: StateFlow<TransferProgress?> = _incomingProgress.asStateFlow()

    private val _transferHistory = MutableStateFlow<List<TransferHistoryEntry>>(emptyList())
    /** Most-recent-first list of completed transfers. */
    val transferHistory: StateFlow<List<TransferHistoryEntry>> = _transferHistory.asStateFlow()

    // ── Internal state ────────────────────────────────────────────────────────

    private var pendingPeerId: String? = null
    private var pendingPeerName: String? = null
    private var pendingPeerStaticB64: String? = null
    private var activeOutput: OutputStream? = null

    private var socket: Socket? = null
    private var sessionJob: Job? = null
    private var session: FerrySession? = null

    // Active incoming transfer (Phase 3A MVP: one at a time)
    private var activeReceiver: FerryTransferReceiver? = null
    private var activeReceiverMeta: TransferMetadata? = null
    private val stagingDir: File get() = File(
        android.os.Environment.getExternalStoragePublicDirectory(
            android.os.Environment.DIRECTORY_DOWNLOADS
        ),
        "Ferry/staging"
    )

    // CompletableDeferred for TRANSFER_ACCEPT/REJECT: resolved by the session loop
    @Volatile private var pendingTransferAccept: CompletableDeferred<Boolean>? = null

    // Cancellation signals for active transfers
    private val outgoingCancelled = java.util.concurrent.atomic.AtomicBoolean(false)
    private val outgoingCancelledByPeer = java.util.concurrent.atomic.AtomicBoolean(false)

    // ── Public API ────────────────────────────────────────────────────────────

    fun cancelOutgoingTransfer(transferId: String? = null) {
        Log.i(TAG, "User requested cancellation of outgoing transfer: $transferId")
        outgoingCancelled.set(true)
        pendingTransferAccept?.complete(false)
        pendingTransferAccept = null
    }

    fun cancelIncomingTransfer(transferId: String? = null) {
        scope.launch {
            val sess = session
            val out = activeOutput
            val currentId = activeReceiverMeta?.transferId ?: transferId ?: return@launch
            Log.i(TAG, "User requested cancellation of incoming transfer: $currentId")
            activeReceiver?.cancel()
            activeReceiver = null
            activeReceiverMeta = null
            _incomingProgress.value = null
            if (sess != null && out != null) {
                try {
                    sendEncryptedMessage(
                        sess, out, ProtocolConstants.MessageTypes.TRANSFER_CANCEL,
                        JSONObject().apply {
                            put("transfer_id", currentId)
                            put("reason", "USER_CANCELLED")
                        }
                    )
                } catch (e: Exception) {
                    Log.w(TAG, "Failed sending TRANSFER_CANCEL: ${e.message}")
                }
            }
        }
    }

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

    // ── Session coroutine ─────────────────────────────────────────────────────

    private suspend fun runSession(device: DiscoveredDevice) = withContext(Dispatchers.IO) {
        val sess = FerrySession(isInitiator = true)
        session = sess

        try {
            sess.transition(FerrySession.State.CONNECTING)
            _sessionState.value = FerrySession.State.CONNECTING

            val sock = Socket(device.host, device.port).also { socket = it }
            sock.soTimeout = 0
            val input = sock.getInputStream()
            val output = sock.getOutputStream()

            sess.transition(FerrySession.State.HANDSHAKING)
            _sessionState.value = FerrySession.State.HANDSHAKING

            // Step 1: Send HANDSHAKE_INIT
            val (_, ephPub, nonce) = sess.buildLocalHandshake(identity.publicKeyBytes)
            val initPayload = JSONObject().apply {
                put("device_id", device.deviceId)
                put("device_name", device.deviceName)
                put("device_type", "mobile")
                put("public_key", identity.publicKeyB64)
                put("ephemeral_key", bytesToB64(ephPub))
                put("nonce", bytesToB64(nonce))
                put("is_paired", false)
            }
            sendPlainFrame(output, ProtocolConstants.MessageTypes.HANDSHAKE_INIT, initPayload)

            // Step 2: Receive HANDSHAKE_RESPONSE
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

            // Step 3: Derive session keys + SAS
            val sas = sess.deriveKeys()
            _sasCode.value = sas

            val remoteDeviceId = remotePayload.optString("device_id", "")
            val remoteDeviceName = remotePayload.optString("device_name", "")
            val remoteStaticBytes = FerryIdentity.decodeB64(remoteStaticB64)

            // Step 4: Authenticate
            sess.transition(FerrySession.State.AUTHENTICATING)
            _sessionState.value = FerrySession.State.AUTHENTICATING

            val transcript = sess.buildAuthTranscript()
            val ourSig = identity.sign(transcript)
            sendPlainFrame(output, ProtocolConstants.MessageTypes.AUTH_CHALLENGE,
                JSONObject().apply { put("signature", bytesToB64(ourSig)) })

            val authResp = recvPlainFrame(input)
            check(authResp.getString("type") == ProtocolConstants.MessageTypes.AUTH_RESPONSE) {
                "Expected AUTH_RESPONSE, got ${authResp.getString("type")}"
            }
            val remoteSig = FerryIdentity.decodeB64(
                authResp.getJSONObject("payload").getString("signature")
            )
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
                Log.i(TAG, "New peer $remoteDeviceName — SAS: $sas")

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

    // ── Pairing ───────────────────────────────────────────────────────────────

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
                sendEncrypted(sess, ProtocolConstants.MessageTypes.PAIR_DECISION,
                    JSONObject().apply { put("decision", "ACCEPT") })
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
                sendEncrypted(sess, ProtocolConstants.MessageTypes.PAIR_DECISION,
                    JSONObject().apply { put("decision", "REJECT") })
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

    // ── Encrypted session loop ────────────────────────────────────────────────

    private fun runEncryptedSessionLoop(sess: FerrySession, input: InputStream, output: OutputStream) {
        this.activeOutput = output
        try {
            while (true) {
                val frameBytes = readAeadFrameBytes(input) ?: break
                val plaintext = sess.decryptFrame(frameBytes)

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
                        } else if (sess.state == FerrySession.State.ESTABLISHED) {
                            try {
                                val payload = JSONObject().apply { put("decision", "ACCEPT") }
                                sendEncrypted(sess, ProtocolConstants.MessageTypes.PAIR_DECISION, payload)
                            } catch (e: Exception) {
                                Log.e(TAG, "Failed to echo pairing acceptance", e)
                            }
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
            activeReceiver?.cancel()
            activeReceiver = null
            activeReceiverMeta = null
            _incomingProgress.value = null
            try { sess.transition(FerrySession.State.CLOSING) } catch (_: Exception) {}
            try { sess.transition(FerrySession.State.DISCONNECTED) } catch (_: Exception) {}
        }
    }

    // ── Transfer message routing ──────────────────────────────────────────────

    private fun onChunkReceived(plaintext: ByteArray) {
        try {
            val (transferId, seq, data) = FerryTransferReceiver.decodeChunkFrame(plaintext)
            val receiver = activeReceiver
            if (receiver == null) {
                Log.w(TAG, "TRANSFER_CHUNK for unknown transfer_id ${transferId.take(8)} (seq=$seq)")
                return
            }
            receiver.receiveChunk(transferId, seq, data)

            // Update incoming progress
            val meta = activeReceiverMeta
            if (meta != null) {
                _incomingProgress.value = TransferProgress(
                    transferId = transferId,
                    fileName = meta.fileName,
                    bytesDone = receiver.bytesReceivedTotal,
                    totalBytes = meta.fileSize,
                    direction = TransferProgress.Direction.INCOMING,
                )
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error processing TRANSFER_CHUNK", e)
            activeReceiver?.cancel()
            activeReceiver = null
            activeReceiverMeta = null
            _incomingProgress.value = null
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
                activeReceiverMeta = null
                _incomingProgress.value = null
                outgoingCancelledByPeer.set(true)
                outgoingCancelled.set(true)
                pendingTransferAccept?.complete(false)
                pendingTransferAccept = null
            }
            ProtocolConstants.MessageTypes.TRANSFER_ERROR -> {
                Log.e(TAG, "TRANSFER_ERROR: [${payload.optString("error_code")}] ${payload.optString("message")}")
                activeReceiver?.cancel()
                activeReceiver = null
                activeReceiverMeta = null
                _incomingProgress.value = null
                pendingTransferAccept?.complete(false)
                pendingTransferAccept = null
            }
            ProtocolConstants.MessageTypes.TRANSFER_ACCEPT -> {
                Log.i(TAG, "TRANSFER_ACCEPT received for ${transferId.take(8)}")
                pendingTransferAccept?.complete(true)
                pendingTransferAccept = null
            }
            ProtocolConstants.MessageTypes.TRANSFER_REJECT -> {
                Log.w(TAG, "TRANSFER_REJECT received for ${transferId.take(8)}")
                pendingTransferAccept?.complete(false)
                pendingTransferAccept = null
            }
            else -> Log.w(TAG, "Unhandled transfer message type: $type")
        }
    }

    private fun onTransferRequest(sess: FerrySession, output: OutputStream, payload: JSONObject) {
        val transferId = payload.optString("transfer_id", "")
        if (activeReceiver != null) {
            Log.w(TAG, "Busy — rejecting transfer ${transferId.take(8)}")
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_REJECT,
                JSONObject().apply { put("transfer_id", transferId); put("reason", "BUSY") })
            return
        }
        try {
            val meta = TransferMetadata.fromJson(payload)
            stagingDir.mkdirs()
            val receiver = FerryTransferReceiver(meta, stagingDir)
            activeReceiver = receiver
            activeReceiverMeta = meta
            receiver.begin()

            // Prime incoming progress
            _incomingProgress.value = TransferProgress(
                transferId = meta.transferId,
                fileName = meta.fileName,
                bytesDone = 0L,
                totalBytes = meta.fileSize,
                direction = TransferProgress.Direction.INCOMING,
            )

            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_ACCEPT,
                JSONObject().apply { put("transfer_id", meta.transferId) })
            Log.i(TAG, "TRANSFER_ACCEPT sent for ${meta.fileName}")
        } catch (e: Exception) {
            Log.e(TAG, "Invalid TRANSFER_REQUEST: ${e.message}")
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_ERROR,
                JSONObject().apply {
                    put("transfer_id", transferId)
                    put("error_code", "INVALID_REQUEST")
                    put("message", e.message ?: "")
                })
        }
    }

    private fun onTransferComplete(sess: FerrySession, output: OutputStream, transferId: String) {
        val receiver = activeReceiver ?: run {
            Log.w(TAG, "TRANSFER_COMPLETE for unknown transfer_id ${transferId.take(8)}")
            return
        }
        val meta = activeReceiverMeta
        val success = receiver.finalise()
        val fileName = meta?.fileName ?: transferId.take(8)
        val fileSize = meta?.fileSize ?: 0L
        activeReceiver = null
        activeReceiverMeta = null
        _incomingProgress.value = null

        sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_RESULT,
            JSONObject().apply {
                put("transfer_id", transferId)
                put("success", success)
                put("sha256", "")
            })

        // Append to history
        appendHistory(
            TransferHistoryEntry(
                transferId = transferId,
                fileName = fileName,
                fileSize = fileSize,
                direction = TransferProgress.Direction.INCOMING,
                success = success,
                timestampMs = System.currentTimeMillis(),
            )
        )
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

    // ── Outgoing Transfers ────────────────────────────────────────────────────

    /**
     * Send a file identified by a SAF [Uri].
     *
     * Opens the URI via [context]'s ContentResolver, resolves file name and size
     * from OpenableColumns, and streams in 64 KiB chunks without loading the full
     * file into memory.
     *
     * Must be called from a coroutine (uses Dispatchers.IO internally).
     */
    suspend fun sendFile(
        uri: Uri,
        context: Context,
        mimeType: String = "application/octet-stream",
    ): Boolean = withContext(Dispatchers.IO) {
        val sess = session
        val out = activeOutput
        if (sess == null || out == null || sess.state != FerrySession.State.ESTABLISHED) {
            Log.e(TAG, "Cannot send file: session not ESTABLISHED")
            return@withContext false
        }

        // Resolve display name and size from ContentResolver
        var fileName = "file"
        var fileSize = -1L
        context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val nameIdx = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val sizeIdx = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (cursor.moveToFirst()) {
                if (nameIdx >= 0) fileName = cursor.getString(nameIdx) ?: "file"
                if (sizeIdx >= 0) fileSize = cursor.getLong(sizeIdx)
            }
        }

        val transferId = UUID.randomUUID().toString()
        val inputStream: InputStream = context.contentResolver.openInputStream(uri)
            ?: run {
                Log.e(TAG, "Cannot open InputStream for $uri")
                return@withContext false
            }

        // Build metadata: we need sha256 upfront — read a first pass only if the file is small,
        // otherwise use an empty placeholder that the receiver will verify on its end.
        // For streaming, we compute sha256 incrementally in streamChunksFromStream and send
        // TRANSFER_COMPLETE without an up-front hash; the receiver's sha256 check uses the
        // hash embedded in TRANSFER_REQUEST. We compute it here via a single read of the
        // InputStream — safe for the streaming chunk path since we use a fresh stream per field.
        val sha256: String
        val freshStreamForHash: InputStream? = context.contentResolver.openInputStream(uri)
        sha256 = if (freshStreamForHash != null) {
            try {
                val digest = java.security.MessageDigest.getInstance("SHA-256")
                val buf = ByteArray(65536)
                var n: Int
                while (freshStreamForHash.read(buf).also { n = it } != -1) {
                    digest.update(buf, 0, n)
                }
                digest.digest().joinToString("") { "%02x".format(it) }
            } catch (e: Exception) {
                Log.e(TAG, "SHA-256 pre-computation failed", e)
                "0".repeat(64)
            } finally {
                freshStreamForHash.close()
            }
        } else "0".repeat(64)

        val chunkCount = if (fileSize > 0) ((fileSize + FerryTransferClient.CHUNK_SIZE - 1) / FerryTransferClient.CHUNK_SIZE).toInt() else 0
        val payload = JSONObject().apply {
            put("transfer_id", transferId)
            put("file_name", fileName)
            put("file_size", fileSize)
            put("mime_type", mimeType)
            put("sha256", sha256)
            put("chunk_size", FerryTransferClient.CHUNK_SIZE)
            put("chunk_count", chunkCount)
            put("sender_identity", identity.publicKeyB64)
            put("created_at", System.currentTimeMillis())
        }

        // Reset cancellation flags for this transfer
        outgoingCancelled.set(false)
        outgoingCancelledByPeer.set(false)

        // Initialise progress
        _transferProgress.value = TransferProgress(
            transferId = transferId,
            fileName = fileName,
            bytesDone = 0L,
            totalBytes = fileSize,
            direction = TransferProgress.Direction.OUTGOING,
        )

        Log.i(TAG, "Sending TRANSFER_REQUEST for $fileName ($fileSize bytes)")
        sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_REQUEST, payload)

        // Wait for TRANSFER_ACCEPT via CompletableDeferred
        val acceptDeferred = CompletableDeferred<Boolean>()
        pendingTransferAccept = acceptDeferred

        Log.i(TAG, "Waiting for TRANSFER_ACCEPT…")
        val accepted = try {
            withContext(Dispatchers.IO) {
                kotlinx.coroutines.withTimeout(60_000L) { acceptDeferred.await() }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Timeout or error waiting for TRANSFER_ACCEPT", e)
            pendingTransferAccept = null
            _transferProgress.value = null
            inputStream.close()
            return@withContext false
        }

        if (!accepted) {
            Log.w(TAG, "Transfer $transferId rejected by peer")
            _transferProgress.value = null
            inputStream.close()
            appendHistory(TransferHistoryEntry(transferId, fileName, fileSize,
                TransferProgress.Direction.OUTGOING, false, System.currentTimeMillis()))
            return@withContext false
        }

        Log.i(TAG, "Transfer accepted — streaming chunks")
        val client = FerryTransferClient()
        var streamSuccess = false
        try {
            val (ok, _) = client.streamChunksFromStream(
                transferId = transferId,
                inputStream = inputStream,
                totalBytes = fileSize,
                encryptAndWrite = { frame ->
                    val encrypted = sess.encryptFrame(frame)
                    out.write(encrypted)
                    out.flush()
                },
                onProgress = { done, total ->
                    _transferProgress.value = TransferProgress(
                        transferId = transferId,
                        fileName = fileName,
                        bytesDone = done,
                        totalBytes = total,
                        direction = TransferProgress.Direction.OUTGOING,
                    )
                },
                cancelSignal = { outgoingCancelled.get() },
            )
            streamSuccess = ok
        } finally {
            inputStream.close()
        }

        if (streamSuccess) {
            Log.i(TAG, "Streaming complete, sending TRANSFER_COMPLETE")
            sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_COMPLETE,
                JSONObject().apply { put("transfer_id", transferId) })
        } else {
            Log.w(TAG, "Streaming cancelled or failed")
            if (!outgoingCancelledByPeer.get()) {
                sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_CANCEL,
                    JSONObject().apply { put("transfer_id", transferId); put("reason", "USER_CANCELLED") })
            }
        }

        _transferProgress.value = null
        appendHistory(TransferHistoryEntry(transferId, fileName, fileSize,
            TransferProgress.Direction.OUTGOING, streamSuccess, System.currentTimeMillis()))

        return@withContext streamSuccess
    }

    /**
     * Legacy sendFile for testing with a pre-loaded byte array.
     * Used by unit tests only.
     */
    suspend fun sendFile(fileBytes: ByteArray, fileName: String, mimeType: String): Boolean =
        withContext(Dispatchers.IO) {
            val sess = session
            val out = activeOutput
            if (sess == null || out == null || sess.state != FerrySession.State.ESTABLISHED) {
                Log.e(TAG, "Cannot send file: session not ESTABLISHED")
                return@withContext false
            }

            val transferId = UUID.randomUUID().toString()
            val sha256 = FerryTransferClient.sha256Hex(fileBytes)
            val chunkSize = FerryTransferClient.CHUNK_SIZE
            val chunkCount = if (fileBytes.isNotEmpty()) (fileBytes.size + chunkSize - 1) / chunkSize else 0

            val payload = JSONObject().apply {
                put("transfer_id", transferId)
                put("file_name", fileName)
                put("file_size", fileBytes.size)
                put("mime_type", mimeType)
                put("sha256", sha256)
                put("chunk_size", chunkSize)
                put("chunk_count", chunkCount)
                put("sender_identity", identity.publicKeyB64)
                put("created_at", System.currentTimeMillis())
            }

            val acceptDeferred = CompletableDeferred<Boolean>()
            pendingTransferAccept = acceptDeferred
            sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_REQUEST, payload)

            val accepted = try {
                kotlinx.coroutines.withTimeout(60_000L) { acceptDeferred.await() }
            } catch (e: Exception) {
                pendingTransferAccept = null
                return@withContext false
            }

            if (!accepted) return@withContext false

            val client = FerryTransferClient()
            val streamResult = client.streamChunks(
                transferId = transferId,
                fileBytes = fileBytes,
                encryptAndWrite = { frame ->
                    val encrypted = sess.encryptFrame(frame)
                    out.write(encrypted)
                    out.flush()
                },
                cancelSignal = { outgoingCancelled.get() },
            )

            if (streamResult) {
                sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_COMPLETE,
                    JSONObject().apply { put("transfer_id", transferId) })
            } else {
                sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_CANCEL,
                    JSONObject().apply { put("transfer_id", transferId); put("reason", "ABORTED") })
            }

            return@withContext streamResult
        }

    // ── History ───────────────────────────────────────────────────────────────

    private fun appendHistory(entry: TransferHistoryEntry) {
        val current = _transferHistory.value.toMutableList()
        current.add(0, entry)
        if (current.size > 50) current.removeAt(current.lastIndex)
        _transferHistory.value = current
    }

    // ── Plain frame I/O ───────────────────────────────────────────────────────

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

    // ── AEAD frame I/O ────────────────────────────────────────────────────────

    private fun readAeadFrameBytes(input: InputStream): ByteArray? {
        val header = input.readNBytes(4)
        if (header.isEmpty()) return null
        check(header.size == 4) { "Connection closed during AEAD header read" }

        val ctLen = ByteBuffer.wrap(header).order(ByteOrder.BIG_ENDIAN).int
        check(ctLen in 0..MAX_FRAME) { "AEAD frame ciphertext length out of range: $ctLen" }

        val rest = input.readNBytes(12 + ctLen)
        check(rest.size == 12 + ctLen) { "Connection closed during AEAD payload read" }

        return header + rest
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    private fun bytesToB64(bytes: ByteArray): String =
        Base64.encodeToString(bytes, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)

    private fun closeSocket() {
        try { socket?.close() } catch (_: Exception) {}
        socket = null
    }
}
