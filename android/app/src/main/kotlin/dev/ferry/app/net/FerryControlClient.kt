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
import dev.ferry.app.transfer.InterruptedTransferStore
import dev.ferry.app.transfer.TransferMetadata
import dev.ferry.app.transfer.TransferState
import dev.ferry.app.service.FerryTransferService
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
    private val context: android.content.Context,
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
        /** Non-null during a batch transfer; null for single-file transfers. */
        val batchInfo: BatchInfo? = null,
    ) {
        enum class Direction { OUTGOING, INCOMING }

        val fraction: Float get() =
            if (totalBytes > 0) (bytesDone.toFloat() / totalBytes).coerceIn(0f, 1f) else (if (bytesDone >= totalBytes) 1f else 0f)
    }

    /** Aggregate context for a batch transfer shown in the progress UI. */
    data class BatchInfo(
        val batchId: String,
        val batchName: String,
        val doneItems: Int,
        val totalItems: Int,
    )

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

    private val _pendingIncomingRequest = MutableStateFlow<TransferMetadata?>(null)
    /** Pending incoming transfer metadata requiring user acceptance. */
    val pendingIncomingRequest: StateFlow<TransferMetadata?> = _pendingIncomingRequest.asStateFlow()

    private val _transferHistory = MutableStateFlow<List<TransferHistoryEntry>>(emptyList())
    /** Most-recent-first list of completed transfers. */
    val transferHistory: StateFlow<List<TransferHistoryEntry>> = _transferHistory.asStateFlow()

    /** Phase 3E: interrupted (resumable) transfers; updated on peer reconnect and on interrupt. */
    private val _interruptedTransfers = MutableStateFlow<List<InterruptedTransferStore.InterruptedRecord>>(emptyList())
    val interruptedTransfers: StateFlow<List<InterruptedTransferStore.InterruptedRecord>> = _interruptedTransfers.asStateFlow()

    /** Clipboard sync: latest text received from the remote peer (null = nothing yet). */
    private val _remoteClipboard = MutableStateFlow<String?>(null)
    val remoteClipboard: StateFlow<String?> = _remoteClipboard.asStateFlow()

    /** Whether clipboard sync is enabled by the user. */
    @Volatile var clipboardSyncEnabled: Boolean = false
    /** Whether incoming transfers should be automatically accepted. */
    @Volatile var autoAcceptTransfers: Boolean = false
    /** Last text pushed *to* the peer — loop guard. */
    @Volatile private var lastClipboardSent: String = ""
    /** Last text received *from* the peer — loop guard. */
    @Volatile private var lastClipboardReceived: String = ""

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
    @Volatile private var pendingBatchAccept: CompletableDeferred<Boolean>? = null
    @Volatile private var pendingIncomingTransferAccept: CompletableDeferred<Boolean>? = null

    // Cancellation signals for active transfers
    private val outgoingCancelled = java.util.concurrent.atomic.AtomicBoolean(false)
    private val outgoingCancelledByPeer = java.util.concurrent.atomic.AtomicBoolean(false)

    // Phase 3D: transfer IDs that have already been processed to terminal state (prevents duplicate handling)
    private val handledTransferIds = java.util.Collections.synchronizedSet(mutableSetOf<String>())

    // Phase 3E: interrupted transfer persistence
    private val interruptedStore: InterruptedTransferStore by lazy { InterruptedTransferStore(context) }

    // Phase 3E: CompletableDeferred for TRANSFER_RESUME_ACCEPT/REJECT
    @Volatile private var pendingResumeAccept: CompletableDeferred<Boolean>? = null
    // Phase 3E: peer static key at the time the session was established (for interrupt records)
    @Volatile private var connectedPeerStaticB64: String? = null

    // Phase 4C: active incoming batch tracking
    @Volatile private var activeBatchId: String? = null
    @Volatile private var activeBatchItemsReceived: Int = 0
    @Volatile private var activeBatchTotalItems: Int = 0

    // Phase 4C: active outgoing batch tracking (for progress overlay)
    @Volatile private var activeBatchName: String = ""
    @Volatile private var activeBatchTotalItemsOut: Int = 0
    @Volatile private var activeBatchSentItems: Int = 0

    // Phase 5: notification mirroring
    /** True when the connected peer has advertised the "notify.v1" capability. */
    @Volatile var peerSupportsNotify: Boolean = false
        private set

    // ── Public API ────────────────────────────────────────────────────────────

    /**
     * Send a NOTIFICATION_POST to the peer (Phase 5).
     * Only sent when session is ESTABLISHED and peer supports notify.v1.
     * title/body are NOT logged.
     */
    fun sendNotificationPost(
        ferryId: String, packageName: String, appLabel: String,
        title: String, body: String, postedAt: Long, category: String,
    ) {
        val sess = session ?: return
        val out  = activeOutput ?: return
        if (sess.state != FerrySession.State.ESTABLISHED) return
        scope.launch {
            try {
                sendEncryptedMessage(
                    sess, out,
                    ProtocolConstants.MessageTypes.NOTIFICATION_POST,
                    JSONObject().apply {
                        put("ferry_id",  ferryId)
                        put("package",   packageName)
                        put("app_label", appLabel)
                        put("title",     title)
                        put("body",      body)
                        put("posted_at", postedAt)
                        put("category",  category)
                    }
                )
            } catch (e: Exception) {
                Log.d(TAG, "Failed to send NOTIFICATION_POST: ${e.message}")
            }
        }
    }

    /**
     * Send a NOTIFICATION_REMOVE to the peer (Phase 5).
     * Only sent when session is ESTABLISHED and peer supports notify.v1.
     */
    fun sendNotificationRemove(ferryId: String) {
        val sess = session ?: return
        val out  = activeOutput ?: return
        if (sess.state != FerrySession.State.ESTABLISHED) return
        scope.launch {
            try {
                sendEncryptedMessage(
                    sess, out,
                    ProtocolConstants.MessageTypes.NOTIFICATION_REMOVE,
                    JSONObject().apply { put("ferry_id", ferryId) }
                )
            } catch (e: Exception) {
                Log.d(TAG, "Failed to send NOTIFICATION_REMOVE: ${e.message}")
            }
        }
    }


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
            val isKnownLocally = trustStore.listPeers().any { it.deviceId == device.deviceId }
            val (_, ephPub, nonce) = sess.buildLocalHandshake(identity.publicKeyBytes)
            val initPayload = JSONObject().apply {
                put("device_id", device.deviceId)
                put("device_name", device.deviceName)
                put("device_type", "mobile")
                put("public_key", identity.publicKeyB64)
                put("ephemeral_key", bytesToB64(ephPub))
                put("nonce", bytesToB64(nonce))
                put("is_paired", isKnownLocally)
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

            val isTrustedLocally = trustStore.isKnownPeer(remoteStaticB64)
            val remoteIsPaired = remotePayload.optBoolean("is_paired", false)
            val isTrusted = isTrustedLocally && remoteIsPaired

            if (isTrusted) {
                connectedPeerStaticB64 = remoteStaticB64
                sess.transition(FerrySession.State.ESTABLISHED)
                _sessionState.value = FerrySession.State.ESTABLISHED
                Log.i(TAG, "Session ESTABLISHED with $remoteDeviceName")
                // Phase 3E: load interrupted transfers from this peer
                val interrupted = interruptedStore.listForSender(remoteStaticB64)
                if (interrupted.isNotEmpty()) {
                    Log.i(TAG, "Found ${interrupted.size} interrupted transfer(s) from $remoteDeviceName")
                    _interruptedTransfers.value = interrupted
                }
                
                // Phase 5: advertise our capabilities to the peer
                try {
                    val capsPayload = JSONObject().apply {
                        val arr = org.json.JSONArray()
                        arr.put("notify.v1")
                        put("capabilities", arr)
                    }
                    sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.CAPABILITIES, capsPayload)
                } catch (e: Exception) {
                    Log.d(TAG, "Failed to send CAPABILITIES: ${e.message}")
                }
                
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
            
            // Phase 5: advertise our capabilities to the peer
            try {
                val capsPayload = JSONObject().apply {
                    val arr = org.json.JSONArray()
                    arr.put("notify.v1")
                    put("capabilities", arr)
                }
                sendEncrypted(sess, ProtocolConstants.MessageTypes.CAPABILITIES, capsPayload)
            } catch (e: Exception) {
                Log.d(TAG, "Failed to send CAPABILITIES: ${e.message}")
            }
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

    fun acceptIncomingTransfer() {
        Log.i(TAG, "User accepted incoming transfer")
        pendingIncomingTransferAccept?.complete(true)
    }

    fun rejectIncomingTransfer() {
        Log.i(TAG, "User rejected incoming transfer")
        pendingIncomingTransferAccept?.complete(false)
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
            // Phase 3E: Interrupt-on-disconnect for eligible incoming transfers.
            val receiver = activeReceiver
            val interruptedMeta = activeReceiverMeta
            if (receiver != null && interruptedMeta != null) {
                val interrupted = receiver.interrupt()
                val peerKey = connectedPeerStaticB64 ?: ""
                if (interrupted >= 0L && peerKey.isNotBlank()) {
                    // Eligible for resume: persist the record.
                    Log.i(
                        TAG,
                        "Session dropped during incoming transfer ${interruptedMeta.transferId.take(8)} " +
                            "— INTERRUPTED at $interrupted bytes, persisting for resume"
                    )
                    val nowMs = System.currentTimeMillis()
                    val record = InterruptedTransferStore.InterruptedRecord(
                        transferId = interruptedMeta.transferId,
                        fileName = interruptedMeta.fileName,
                        fileSize = interruptedMeta.fileSize,
                        mimeType = interruptedMeta.mimeType,
                        sha256 = interruptedMeta.sha256,
                        chunkSize = interruptedMeta.chunkSize,
                        chunkCount = interruptedMeta.chunkCount,
                        senderIdentity = peerKey,
                        receiverIdentity = identity.publicKeyB64,
                        originalMetadataJson = interruptedMeta.toJson().toString(),
                        bytesReceived = interrupted,
                        resumeChunkIndex = (interrupted / interruptedMeta.chunkSize).toInt(),
                        partialSha256 = "",   // populated by requestResume() on demand
                        interruptedAt = nowMs,
                        expireAt = InterruptedTransferStore.makeExpireAt(nowMs),
                        direction = InterruptedTransferStore.InterruptedRecord.Direction.INCOMING,
                        contentUriString = null,
                    )
                    interruptedStore.save(record)
                    _interruptedTransfers.value = interruptedStore.listForSender(peerKey)
                } else {
                    // Not eligible: cancel normally and record FAILED history.
                    Log.w(TAG, "Session dropped during incoming transfer ${interruptedMeta.transferId.take(8)} — marking FAILED")
                    appendHistory(
                        TransferHistoryEntry(
                            transferId = interruptedMeta.transferId,
                            fileName = interruptedMeta.fileName,
                            fileSize = interruptedMeta.fileSize,
                            direction = TransferProgress.Direction.INCOMING,
                            success = false,
                            timestampMs = System.currentTimeMillis(),
                        )
                    )
                    activeReceiver?.cancel()
                }
            } else {
                activeReceiver?.cancel()
            }
            activeReceiver = null
            activeReceiverMeta = null
            _incomingProgress.value = null
            // Phase 3D: Unblock any sendFile() waiting for ACCEPT
            pendingTransferAccept?.complete(false)
            pendingTransferAccept = null
            // Phase 3E: Unblock any pending resume accept
            pendingResumeAccept?.complete(false)
            pendingResumeAccept = null
            // Phase 3D: Clear outgoing progress
            _transferProgress.value = null
            connectedPeerStaticB64 = null
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
            
            // Phase 4C: Batch Handlers
            ProtocolConstants.MessageTypes.BATCH_REQUEST -> {
                onBatchRequest(sess, output, payload)
            }
            ProtocolConstants.MessageTypes.BATCH_ACCEPT -> {
                pendingBatchAccept?.complete(true)
            }
            ProtocolConstants.MessageTypes.BATCH_REJECT -> {
                pendingBatchAccept?.complete(false)
            }
            ProtocolConstants.MessageTypes.BATCH_CANCEL -> {
                onBatchCancel(sess, output, payload.optString("batch_id", ""))
            }
            ProtocolConstants.MessageTypes.BATCH_COMPLETE -> {
                onBatchComplete(sess, output, payload.optString("batch_id", ""))
            }
            ProtocolConstants.MessageTypes.TRANSFER_CANCEL -> {
                val tid = payload.optString("transfer_id", "")
                Log.i(TAG, "TRANSFER_CANCEL received for ${tid.take(8)}")
                // Phase 3D: idempotent — ignore if already terminal
                if (tid.isNotEmpty() && handledTransferIds.contains(tid)) {
                    Log.d(TAG, "Duplicate TRANSFER_CANCEL for $tid — ignoring")
                    pendingTransferAccept?.complete(false)
                    pendingTransferAccept = null
                    return
                }
                if (tid.isNotEmpty()) handledTransferIds.add(tid)
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
                // Phase 3D: duplicate guard
                if (transferId.isNotEmpty() && handledTransferIds.contains(transferId)) {
                    Log.d(TAG, "Duplicate TRANSFER_ACCEPT for $transferId — ignoring")
                    return
                }
                if (transferId.isNotEmpty()) handledTransferIds.add(transferId)
                pendingTransferAccept?.complete(true)
                pendingTransferAccept = null
            }
            ProtocolConstants.MessageTypes.TRANSFER_REJECT -> {
                Log.w(TAG, "TRANSFER_REJECT received for ${transferId.take(8)}")
                // Phase 3D: duplicate guard
                if (transferId.isNotEmpty() && handledTransferIds.contains(transferId)) {
                    Log.d(TAG, "Duplicate TRANSFER_REJECT for $transferId — ignoring")
                    return
                }
                if (transferId.isNotEmpty()) handledTransferIds.add(transferId)
                pendingTransferAccept?.complete(false)
                pendingTransferAccept = null
            }
            // Phase 3E: sender receives TRANSFER_RESUME_REQUEST from receiver
            ProtocolConstants.MessageTypes.TRANSFER_RESUME_REQUEST -> {
                val tid = payload.optString("transfer_id", "")
                val resumeChunkIndex = payload.optInt("resume_chunk_index", 0)
                val resumeOffsetBytes = payload.optLong("resume_offset_bytes", 0L)
                val partialSha256 = payload.optString("partial_sha256", "")
                Log.i(TAG, "TRANSFER_RESUME_REQUEST for ${tid.take(8)} (chunk=$resumeChunkIndex, offset=$resumeOffsetBytes)")
                // Android is sender side — we respond on a background coroutine
                // (streaming is blocking I/O and must not block the message loop).
                scope.launch {
                    onResumeRequest(sess, output, tid, resumeChunkIndex, resumeOffsetBytes.toInt(), partialSha256)
                }
            }

            // Phase 3E: receiver receives TRANSFER_RESUME_ACCEPT from sender
            ProtocolConstants.MessageTypes.TRANSFER_RESUME_ACCEPT -> {
                val tid = payload.optString("transfer_id", "")
                val resumeChunkIndex = payload.optInt("resume_chunk_index", 0)
                Log.i(TAG, "TRANSFER_RESUME_ACCEPT for ${tid.take(8)} (chunk=$resumeChunkIndex)")
                val receiver = activeReceiver
                if (receiver != null && activeReceiverMeta?.transferId == tid) {
                    try {
                        receiver.resume(resumeChunkIndex)
                        pendingResumeAccept?.complete(true)
                        pendingResumeAccept = null
                        // Remove from interrupted store — we are now actively resuming.
                        interruptedStore.delete(tid)
                        _interruptedTransfers.value = interruptedStore.listForSender(connectedPeerStaticB64 ?: "")
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to resume receiver for $tid: ${e.message}")
                        activeReceiver?.cancel()
                        activeReceiver = null
                        activeReceiverMeta = null
                        _incomingProgress.value = null
                        pendingResumeAccept?.complete(false)
                        pendingResumeAccept = null
                    }
                } else {
                    Log.w(TAG, "TRANSFER_RESUME_ACCEPT for unknown/unregistered transfer ${tid.take(8)}")
                    pendingResumeAccept?.complete(true)
                    pendingResumeAccept = null
                }
            }

            // Phase 3E: receiver receives TRANSFER_RESUME_REJECT from sender
            ProtocolConstants.MessageTypes.TRANSFER_RESUME_REJECT -> {
                val tid = payload.optString("transfer_id", "")
                val reason = payload.optString("reason", "UNKNOWN")
                Log.w(TAG, "TRANSFER_RESUME_REJECT for ${tid.take(8)} (reason=$reason)")
                // Clean up receiver and .part file
                activeReceiver?.cancel()
                activeReceiver = null
                val meta = activeReceiverMeta
                activeReceiverMeta = null
                _incomingProgress.value = null
                // Record as FAILED
                if (meta != null) {
                    appendHistory(TransferHistoryEntry(
                        transferId = tid,
                        fileName = meta.fileName,
                        fileSize = meta.fileSize,
                        direction = TransferProgress.Direction.INCOMING,
                        success = false,
                        timestampMs = System.currentTimeMillis(),
                    ))
                }
                // Remove from interrupted store
                interruptedStore.delete(tid)
                _interruptedTransfers.value = interruptedStore.listForSender(connectedPeerStaticB64 ?: "")
                pendingResumeAccept?.complete(false)
                pendingResumeAccept = null
            }

            ProtocolConstants.MessageTypes.CLIPBOARD_SYNC -> {
                onClipboardSync(payload)
            }

            ProtocolConstants.MessageTypes.CLIPBOARD_SYNC_ACK -> { /* no-op */ }

            ProtocolConstants.MessageTypes.CAPABILITIES -> {
                // Phase 5: parse peer capabilities
                val caps = mutableListOf<String>()
                val arr = payload.optJSONArray("capabilities")
                if (arr != null) {
                    for (i in 0 until arr.length()) {
                        caps.add(arr.optString(i, ""))
                    }
                }
                val hadNotify = peerSupportsNotify
                peerSupportsNotify = caps.contains("notify.v1")
                Log.i(TAG, "Peer capabilities: $caps (notify.v1=${peerSupportsNotify})")
                // If peer now supports notify, flush any buffered notifications
                if (!hadNotify && peerSupportsNotify) {
                    dev.ferry.app.notification.FerryNotificationListenerService.dispatcher?.flush()
                }
            }

            // Phase 5: Android receives no notification messages from Linux in this direction.
            ProtocolConstants.MessageTypes.NOTIFICATION_POST,
            ProtocolConstants.MessageTypes.NOTIFICATION_REMOVE -> {
                Log.d(TAG, "Received $type from peer (no-op on Android)")
            }

            else -> Log.w(TAG, "Unhandled transfer message type: $type")
        }
    }

    // ── Clipboard Sync ────────────────────────────────────────────────────────

    /**
     * Handle an incoming CLIPBOARD_SYNC message from the peer.
     * Exposes the text via [remoteClipboard] flow; the UI layer is
     * responsible for writing it to the Android ClipboardManager.
     */
    private fun onClipboardSync(payload: JSONObject) {
        return // Disabled per final product scope
        val text = payload.optString("text", "")
        if (text.isEmpty() || text == lastClipboardReceived) return
        if (text.toByteArray(Charsets.UTF_8).size > 524_288) {
            Log.w(TAG, "Incoming clipboard too large — discarding")
            return
        }
        lastClipboardReceived = text
        lastClipboardSent = text   // loop guard: don't echo back
        _remoteClipboard.value = text
        Log.d(TAG, "Clipboard sync received (${text.length} chars)")
    }

    /**
     * Push [text] to the connected peer as a CLIPBOARD_SYNC message.
     * No-op if clipboard sync is disabled, text is empty, identical to the
     * last text we sent, or the same text we just received (loop guard).
     */
    fun sendClipboardSync(text: String) {
        return // Disabled per final product scope
        if (text.isEmpty()) return
        if (text.toByteArray(Charsets.UTF_8).size > 524_288) return
        if (text == lastClipboardSent) return
        if (text == lastClipboardReceived) return  // loop guard
        val sess = session ?: return
        val out  = activeOutput ?: return
        lastClipboardSent = text
        scope.launch {
            try {
                val payload = JSONObject().apply {
                    put("text", text)
                    put("ts", System.currentTimeMillis())
                }
                withContext(Dispatchers.IO) {
                    sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.CLIPBOARD_SYNC, payload)
                }
                Log.d(TAG, "Clipboard sync sent (${text.length} chars)")
            } catch (e: Exception) {
                Log.d(TAG, "Failed to send clipboard sync: ${e.message}")
            }
        }
    }

    private fun onBatchRequest(sess: FerrySession, output: OutputStream, payload: JSONObject) {
        val batchId = payload.optString("batch_id", "")
        val batchName = payload.optString("batch_name", "Batch")
        val totalItems = payload.optInt("total_items", 0)
        Log.i(TAG, "Received BATCH_REQUEST: $batchName ($totalItems items) batchId=${batchId.take(8)}")

        if (batchId.isEmpty() || totalItems <= 0) {
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.BATCH_REJECT, JSONObject().apply {
                put("batch_id", batchId)
                put("reason", "INVALID_REQUEST")
            })
            return
        }

        // Track the active batch so onTransferRequest auto-accepts items belonging to it
        activeBatchId = batchId
        activeBatchItemsReceived = 0
        activeBatchTotalItems = totalItems

        // Auto-accept: batch is always accepted when we have no competing transfer
        sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.BATCH_ACCEPT, JSONObject().apply {
            put("batch_id", batchId)
        })
        Log.i(TAG, "BATCH_ACCEPT sent for batchId=${batchId.take(8)}")
    }

    private fun onBatchCancel(sess: FerrySession, output: OutputStream, batchId: String) {
        Log.i(TAG, "Received BATCH_CANCEL for ${batchId.take(8)}")
        outgoingCancelledByPeer.set(true)
        pendingBatchAccept?.complete(false)
        pendingBatchAccept = null
        // Clear incoming batch state too
        if (batchId == activeBatchId) {
            activeBatchId = null
            activeBatchItemsReceived = 0
            activeBatchTotalItems = 0
        }
    }

    private fun onBatchComplete(sess: FerrySession, output: OutputStream, batchId: String) {
        Log.i(TAG, "Received BATCH_COMPLETE for ${batchId.take(8)} (received $activeBatchItemsReceived/$activeBatchTotalItems)")
        if (batchId == activeBatchId) {
            activeBatchId = null
            activeBatchItemsReceived = 0
            activeBatchTotalItems = 0
        }
    }

    private fun onTransferRequest(sess: FerrySession, output: OutputStream, payload: JSONObject) {
        val transferId = payload.optString("transfer_id", "")
        val batchId = payload.optString("batch_id", "")
        val isBatched = batchId.isNotEmpty() && batchId == activeBatchId

        // Reject if busy with single transfer and this is not a batched item
        if (activeReceiver != null && !isBatched) {
            Log.w(TAG, "Busy — rejecting transfer ${transferId.take(8)}")
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_REJECT,
                JSONObject().apply { put("transfer_id", transferId); put("reason", "BUSY") })
            return
        }
        // Reject batched item if batch ID is not recognised
        if (batchId.isNotEmpty() && batchId != activeBatchId) {
            Log.w(TAG, "Rejecting transfer ${transferId.take(8)} — unknown batch $batchId")
            sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_REJECT,
                JSONObject().apply { put("transfer_id", transferId); put("reason", "INVALID_BATCH") })
            return
        }
        try {
            val meta = TransferMetadata.fromJson(payload)
            stagingDir.mkdirs()
            val receiver = FerryTransferReceiver(meta, stagingDir)
            // Phase 3D: guard begin() — failure to open staging file should not crash the session loop
            try {
                receiver.begin()
            } catch (e: java.io.IOException) {
                Log.e(TAG, "Cannot begin transfer ${meta.transferId.take(8)}: ${e.message}")
                sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_ERROR,
                    JSONObject().apply {
                        put("transfer_id", meta.transferId)
                        put("error_code", "ERR_IO_FAILURE")
                        put("message", e.message ?: "staging file creation failed")
                    })
                return
            }
            activeReceiver = receiver
            activeReceiverMeta = meta

            // Prime incoming progress
            _incomingProgress.value = TransferProgress(
                transferId = meta.transferId,
                fileName = meta.fileName,
                bytesDone = 0L,
                totalBytes = meta.fileSize,
                direction = TransferProgress.Direction.INCOMING,
            )

            if (isBatched || autoAcceptTransfers) {
                sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_ACCEPT,
                    JSONObject().apply { put("transfer_id", meta.transferId) })
                Log.i(TAG, "TRANSFER_ACCEPT auto-sent for ${meta.fileName}")
            } else {
                Log.i(TAG, "Waiting for user to accept incoming transfer ${meta.fileName}")
                val acceptDeferred = CompletableDeferred<Boolean>()
                pendingIncomingTransferAccept = acceptDeferred
                _pendingIncomingRequest.value = meta
                
                scope.launch {
                    val accepted = try {
                        kotlinx.coroutines.withTimeout(300_000L) { acceptDeferred.await() }
                    } catch (e: Exception) {
                        false
                    } finally {
                        pendingIncomingTransferAccept = null
                        _pendingIncomingRequest.value = null
                    }
                    
                    if (accepted) {
                        sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_ACCEPT,
                            JSONObject().apply { put("transfer_id", meta.transferId) })
                        Log.i(TAG, "TRANSFER_ACCEPT manually sent for ${meta.fileName}")
                    } else {
                        activeReceiver?.cancel()
                        activeReceiver = null
                        activeReceiverMeta = null
                        _incomingProgress.value = null
                        sendEncryptedMessage(sess, output, ProtocolConstants.MessageTypes.TRANSFER_REJECT,
                            JSONObject().apply { put("transfer_id", meta.transferId); put("reason", "USER_REJECTED") })
                        Log.i(TAG, "TRANSFER_REJECT manually sent for ${meta.fileName}")
                    }
                }
            }
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
        // Phase 3D: duplicate guard
        if (transferId.isNotEmpty() && handledTransferIds.contains(transferId)) {
            Log.d(TAG, "Duplicate TRANSFER_COMPLETE for $transferId — ignoring")
            return
        }
        if (transferId.isNotEmpty()) handledTransferIds.add(transferId)

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

    // ── Phase 3E Resume APIs ──────────────────────────────────────────────────

    /**
     * Phase 3E public API — Request resumption of an interrupted incoming transfer.
     *
     * Reconstructs a FerryTransferReceiver in INTERRUPTED state from the stored
     * InterruptedRecord, computes the partial_sha256 of the .part file, sends
     * TRANSFER_RESUME_REQUEST, and waits up to 120 seconds for ACCEPT or REJECT.
     *
     * Must be called from a coroutine. Returns true if the sender accepted the
     * resume and streaming has begun, false otherwise.
     */
    suspend fun requestResume(record: InterruptedTransferStore.InterruptedRecord): Boolean =
        withContext(Dispatchers.IO) {
            val sess = session ?: run {
                Log.e(TAG, "Cannot resume: no active session")
                return@withContext false
            }
            val out = activeOutput ?: run {
                Log.e(TAG, "Cannot resume: no output stream")
                return@withContext false
            }
            if (sess.state != FerrySession.State.ESTABLISHED) {
                Log.e(TAG, "Cannot resume: session not ESTABLISHED")
                return@withContext false
            }

            // Rebuild the TransferMetadata from the stored JSON.
            val meta = try {
                TransferMetadata.fromJson(org.json.JSONObject(record.originalMetadataJson))
            } catch (e: Exception) {
                Log.e(TAG, "Cannot parse metadata for interrupted transfer ${record.transferId.take(8)}: ${e.message}")
                return@withContext false
            }

            // Check that the .part file still exists.
            val partFile = java.io.File(stagingDir, "${record.transferId}.part")
            if (!partFile.exists()) {
                Log.e(TAG, "Cannot resume: .part file missing for ${record.transferId.take(8)}")
                interruptedStore.delete(record.transferId)
                _interruptedTransfers.value = interruptedStore.listForSender(connectedPeerStaticB64 ?: "")
                return@withContext false
            }

            // Compute partial_sha256 from the .part file.
            val partialSha256 = computePrefixSha256(partFile)
            if (partialSha256 == null) {
                Log.e(TAG, "Cannot compute partial_sha256 for ${record.transferId.take(8)}")
                return@withContext false
            }

            // Reconstruct receiver in INTERRUPTED state so the TRANSFER_RESUME_ACCEPT
            // handler can call receiver.resume(chunkIndex) on it.
            // begin() calls createNewFile() which is idempotent (false if .part already exists).
            val newReceiver = FerryTransferReceiver(meta, stagingDir)
            try {
                newReceiver.begin()   // IDLE -> TRANSFERRING (creates/preserves .part)
            } catch (e: java.io.IOException) {
                Log.w(TAG, "begin() in requestResume returned IO warning (ok if .part exists): ${e.message}")
                // Non-fatal: .part already exists from previous session
            }
            newReceiver.interrupt()  // TRANSFERRING -> INTERRUPTED
            activeReceiver = newReceiver
            activeReceiverMeta = meta

            // Set a deferred to wait for ACCEPT/REJECT from the message loop.
            val deferred = CompletableDeferred<Boolean>()
            pendingResumeAccept = deferred

            // Send TRANSFER_RESUME_REQUEST.
            try {
                sendEncryptedMessage(sess, out,
                    ProtocolConstants.MessageTypes.TRANSFER_RESUME_REQUEST,
                    JSONObject().apply {
                        put("transfer_id", record.transferId)
                        put("resume_chunk_index", record.resumeChunkIndex)
                        put("resume_offset_bytes", record.bytesReceived)
                        put("partial_sha256", partialSha256)
                        put("protocol_version", 1)
                    }
                )
                Log.i(TAG, "TRANSFER_RESUME_REQUEST sent for ${record.transferId.take(8)} " +
                    "(chunk=${record.resumeChunkIndex}, offset=${record.bytesReceived})")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to send TRANSFER_RESUME_REQUEST: ${e.message}")
                activeReceiver?.cancel()
                activeReceiver = null
                activeReceiverMeta = null
                pendingResumeAccept = null
                return@withContext false
            }

            // Wait up to 120 seconds.
            return@withContext try {
                kotlinx.coroutines.withTimeout(120_000L) { deferred.await() }
            } catch (e: Exception) {
                Log.w(TAG, "Timed out waiting for TRANSFER_RESUME_ACCEPT for ${record.transferId.take(8)}")
                pendingResumeAccept = null
                activeReceiver?.cancel()
                activeReceiver = null
                activeReceiverMeta = null
                false
            }
        }

    /**
     * Phase 3E public API — Discard an interrupted transfer (no longer want to resume).
     * Deletes the .part file and removes the record from InterruptedTransferStore.
     */
    fun discardInterrupted(transferId: String) {
        val partFile = java.io.File(stagingDir, "$transferId.part")
        if (partFile.exists()) {
            partFile.delete()
            Log.i(TAG, "Discarded .part file for interrupted transfer ${transferId.take(8)}")
        }
        interruptedStore.delete(transferId)
        _interruptedTransfers.value = interruptedStore.listForSender(connectedPeerStaticB64 ?: "")
    }

    /**
     * Phase 3E — Sender-side: handle an incoming TRANSFER_RESUME_REQUEST.
     *
     * Verifies the prefix hash, sends TRANSFER_RESUME_ACCEPT, and streams
     * chunks from the resume offset. Called on a background coroutine from
     * the message router to avoid blocking the receive loop.
     */
    private suspend fun onResumeRequest(
        sess: FerrySession,
        output: OutputStream,
        transferId: String,
        resumeChunkIndex: Int,
        resumeOffsetBytes: Int,
        partialSha256: String,
    ) = withContext(Dispatchers.IO) {
        fun reject(reason: String) {
            try {
                sendEncryptedMessage(sess, output,
                    ProtocolConstants.MessageTypes.TRANSFER_RESUME_REJECT,
                    JSONObject().apply {
                        put("transfer_id", transferId)
                        put("reason", reason)
                    }
                )
            } catch (e: Exception) {
                Log.w(TAG, "Failed to send TRANSFER_RESUME_REJECT: ${e.message}")
            }
        }

        // We need the interrupted store record to locate the content URI.
        val record = interruptedStore.get(transferId)
        if (record == null || record.contentUriString == null) {
            Log.w(TAG, "TRANSFER_RESUME_REQUEST for $transferId: no sender record / no content URI")
            reject("TRANSFER_NOT_FOUND")
            return@withContext
        }

        val contentUri = try {
            android.net.Uri.parse(record.contentUriString)
        } catch (e: Exception) {
            Log.w(TAG, "Cannot parse content URI for ${transferId.take(8)}: ${e.message}")
            reject("SOURCE_MODIFIED")
            return@withContext
        }

        // Compute prefix SHA-256 from content URI.
        val computedPrefix = try {
            computePrefixSha256FromUri(contentUri, resumeOffsetBytes.toLong())
        } catch (e: Exception) {
            Log.w(TAG, "Cannot compute prefix SHA-256 for ${transferId.take(8)}: ${e.message}")
            reject("SOURCE_MODIFIED")
            return@withContext
        }

        if (computedPrefix == null || computedPrefix != partialSha256) {
            Log.w(TAG, "Prefix hash mismatch for ${transferId.take(8)}")
            reject("PARTIAL_CORRUPT")
            return@withContext
        }

        // Send ACCEPT.
        try {
            sendEncryptedMessage(sess, output,
                ProtocolConstants.MessageTypes.TRANSFER_RESUME_ACCEPT,
                JSONObject().apply {
                    put("transfer_id", transferId)
                    put("resume_chunk_index", resumeChunkIndex)
                    put("protocol_version", 1)
                }
            )
        } catch (e: Exception) {
            Log.e(TAG, "Failed to send TRANSFER_RESUME_ACCEPT: ${e.message}")
            return@withContext
        }

        // Stream chunks from the resume offset.
        try {
            val inputStream = context.contentResolver.openInputStream(contentUri)
                ?: run {
                    Log.e(TAG, "Cannot open content URI for resume of ${transferId.take(8)}")
                    return@withContext
                }
            inputStream.use { fis ->
                val skipped = fis.skip(resumeOffsetBytes.toLong())
                if (skipped != resumeOffsetBytes.toLong()) {
                    Log.w(TAG, "Could not skip $resumeOffsetBytes bytes for resume of ${transferId.take(8)}")
                    return@withContext
                }
                val client = FerryTransferClient()
                client.streamChunksFromStream(
                    transferId = transferId,
                    inputStream = fis,
                    totalBytes = record.fileSize,
                    startSeq = resumeChunkIndex,
                    encryptAndWrite = { frame ->
                        val encrypted = sess.encryptFrame(frame)
                        output.write(encrypted)
                        output.flush()
                    },
                    onProgress = { done, total ->
                        _transferProgress.value = TransferProgress(
                            transferId = transferId,
                            fileName = record.fileName,
                            bytesDone = resumeOffsetBytes + done,
                            totalBytes = total,
                            direction = TransferProgress.Direction.OUTGOING,
                        )
                    },
                    cancelSignal = { outgoingCancelled.get() },
                )
            }
            // Send TRANSFER_COMPLETE.
            sendEncryptedMessage(sess, output,
                ProtocolConstants.MessageTypes.TRANSFER_COMPLETE,
                JSONObject().apply { put("transfer_id", transferId) }
            )
            Log.i(TAG, "Resumed transfer ${transferId.take(8)}: TRANSFER_COMPLETE sent")
        } catch (e: Exception) {
            Log.e(TAG, "Error during resume streaming for ${transferId.take(8)}: ${e.message}")
        } finally {
            _transferProgress.value = null
            interruptedStore.delete(transferId)
            _interruptedTransfers.value = interruptedStore.listForSender(connectedPeerStaticB64 ?: "")
        }
    }

    /**
     * Compute SHA-256 of all bytes in [file].
     * Used for receiver-side partial_sha256 in TRANSFER_RESUME_REQUEST.
     */
    private fun computePrefixSha256(file: java.io.File): String? = try {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        file.inputStream().use { fis ->
            val buf = ByteArray(65536)
            var n: Int
            while (fis.read(buf).also { n = it } != -1) {
                digest.update(buf, 0, n)
            }
        }
        digest.digest().joinToString("") { "%02x".format(it) }
    } catch (e: Exception) {
        Log.w(TAG, "computePrefixSha256 failed: ${e.message}")
        null
    }

    /**
     * Compute SHA-256 of the first [lengthBytes] bytes of the content identified by [uri].
     * Used for sender-side verification in TRANSFER_RESUME_REQUEST handling.
     */
    private fun computePrefixSha256FromUri(uri: android.net.Uri, lengthBytes: Long): String? {
        return try {
            val digest = java.security.MessageDigest.getInstance("SHA-256")
            context.contentResolver.openInputStream(uri)?.use { fis ->
                var remaining = lengthBytes
                val buf = ByteArray(65536)
                while (remaining > 0) {
                    val toRead = minOf(buf.size.toLong(), remaining).toInt()
                    val n = fis.read(buf, 0, toRead)
                    if (n == -1) return null  // file shorter than expected
                    digest.update(buf, 0, n)
                    remaining -= n
                }
            } ?: return null
            digest.digest().joinToString("") { "%02x".format(it) }
        } catch (e: Exception) {
            Log.w(TAG, "computePrefixSha256FromUri failed: ${e.message}")
            null
        }
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

    // ── Batch Transfers (Phase 4C) ────────────────────────────────────────────

    suspend fun sendBatch(uris: List<Uri>, context: Context, batchName: String): Boolean = withContext(Dispatchers.IO) {
        val sess = session
        val out = activeOutput
        if (sess == null || out == null || sess.state != FerrySession.State.ESTABLISHED) {
            Log.e(TAG, "Cannot send batch: session not ESTABLISHED")
            return@withContext false
        }
        
        if (uris.isEmpty()) return@withContext false

        val batchId = UUID.randomUUID().toString()
        var totalBytes = 0L
        for (uri in uris) {
            context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                val sizeIdx = cursor.getColumnIndex(OpenableColumns.SIZE)
                if (cursor.moveToFirst() && sizeIdx >= 0) {
                    totalBytes += cursor.getLong(sizeIdx)
                }
            }
        }

        val payload = JSONObject().apply {
            put("batch_id", batchId)
            put("batch_name", batchName)
            put("total_items", uris.size)  // protocol field name (matches Linux)
            put("total_bytes", totalBytes)
            put("sender_identity", identity.publicKeyB64)
            put("created_at", System.currentTimeMillis())
        }

        outgoingCancelled.set(false)
        outgoingCancelledByPeer.set(false)

        Log.i(TAG, "Sending BATCH_REQUEST for $batchName ($totalBytes bytes)")
        sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.BATCH_REQUEST, payload)

        val acceptDeferred = CompletableDeferred<Boolean>()
        pendingBatchAccept = acceptDeferred

        val accepted = try {
            kotlinx.coroutines.withTimeout(60_000L) { acceptDeferred.await() }
        } catch (e: Exception) {
            pendingBatchAccept = null
            Log.e(TAG, "Batch request timed out")
            return@withContext false
        }

        if (!accepted) {
            Log.i(TAG, "Batch request rejected by peer")
            return@withContext false
        }

        var success = true
        activeBatchTotalItemsOut = uris.size
        activeBatchSentItems = 0
        activeBatchName = batchName
        for (uri in uris) {
            if (outgoingCancelled.get() || outgoingCancelledByPeer.get()) {
                success = false
                break
            }
            activeBatchSentItems++
            val mimeType = context.contentResolver.getType(uri) ?: "application/octet-stream"
            // For now, no tree traversal, just list of files, so relative_path = ""
            val ok = sendFile(uri, context, mimeType, batchId = batchId, relativePath = "")
            if (!ok) {
                success = false
                break
            }
        }
        activeBatchTotalItemsOut = 0
        activeBatchSentItems = 0
        activeBatchName = ""

        if (success) {
            sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.BATCH_COMPLETE, JSONObject().apply { put("batch_id", batchId) })
        } else {
            sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.BATCH_CANCEL, JSONObject().apply { put("batch_id", batchId) })
        }
        return@withContext success
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
        batchId: String = "",
        relativePath: String = "",
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
            put("batch_id", batchId)
            put("relative_path", relativePath)
        }

        // Reset cancellation flags for this transfer
        outgoingCancelled.set(false)
        outgoingCancelledByPeer.set(false)

        // Initialise progress (with batch context if this is part of a batch)
        val batchContext = if (batchId.isNotEmpty() && activeBatchTotalItemsOut > 0) {
            BatchInfo(
                batchId = batchId,
                batchName = activeBatchName,
                doneItems = activeBatchSentItems,
                totalItems = activeBatchTotalItemsOut,
            )
        } else null
        _transferProgress.value = TransferProgress(
            transferId = transferId,
            fileName = fileName,
            bytesDone = 0L,
            totalBytes = fileSize,
            direction = TransferProgress.Direction.OUTGOING,
            batchInfo = batchContext,
        )

        Log.i(TAG, "Sending TRANSFER_REQUEST for $fileName ($fileSize bytes)")
        sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_REQUEST, payload)

        // Wait for TRANSFER_ACCEPT via CompletableDeferred
        // Phase 3D: Allocate the deferred AFTER resetting flags so a prior cancel flag cannot bleed in
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
        // Start foreground service to keep transfer alive if app is backgrounded
        FerryTransferService.startTransferService(context, fileName)
        val client = FerryTransferClient()
        var streamSuccess = false
        var bytesSent = 0L
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
                    bytesSent = done
                    val batchCtx = if (batchId.isNotEmpty() && activeBatchTotalItemsOut > 0) {
                        BatchInfo(
                            batchId = batchId,
                            batchName = activeBatchName,
                            doneItems = activeBatchSentItems,
                            totalItems = activeBatchTotalItemsOut,
                        )
                    } else null
                    _transferProgress.value = TransferProgress(
                        transferId = transferId,
                        fileName = fileName,
                        bytesDone = done,
                        totalBytes = total,
                        direction = TransferProgress.Direction.OUTGOING,
                        batchInfo = batchCtx,
                    )
                    // Update foreground service notification
                    val pct = if (total > 0) ((done * 100) / total).toInt() else 0
                    val batchLabel = batchCtx?.let { "${it.doneItems}/${it.totalItems}: $fileName" } ?: ""
                    FerryTransferService.updateProgress(context, fileName, pct, batchLabel)
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
            if (!outgoingCancelled.get() && !outgoingCancelledByPeer.get() && bytesSent > 0) {
                // Connection dropped midway. Save for resume.
                val peerKey = connectedPeerStaticB64 ?: ""
                if (peerKey.isNotBlank()) {
                    Log.i(TAG, "Saving interrupted outgoing transfer $transferId at $bytesSent bytes")
                    val nowMs = System.currentTimeMillis()
                    val record = InterruptedTransferStore.InterruptedRecord(
                        transferId = transferId,
                        fileName = fileName,
                        fileSize = fileSize,
                        mimeType = mimeType,
                        sha256 = sha256,
                        chunkSize = FerryTransferClient.CHUNK_SIZE,
                        chunkCount = chunkCount,
                        senderIdentity = identity.publicKeyB64,
                        receiverIdentity = peerKey,
                        originalMetadataJson = payload.toString(),
                        bytesReceived = bytesSent,
                        resumeChunkIndex = (bytesSent / FerryTransferClient.CHUNK_SIZE).toInt(),
                        partialSha256 = "",
                        interruptedAt = nowMs,
                        expireAt = InterruptedTransferStore.makeExpireAt(nowMs),
                        direction = InterruptedTransferStore.InterruptedRecord.Direction.OUTGOING,
                        contentUriString = uri.toString(),
                    )
                    interruptedStore.save(record)
                }
            } else if (!outgoingCancelledByPeer.get()) {
                sendEncryptedMessage(sess, out, ProtocolConstants.MessageTypes.TRANSFER_CANCEL,
                    JSONObject().apply { put("transfer_id", transferId); put("reason", "USER_CANCELLED") })
            }
        }

        _transferProgress.value = null
        // Stop the foreground service (only stops if this was the last transfer in any batch)
        if (batchId.isEmpty() || activeBatchTotalItemsOut == 0) {
            FerryTransferService.stopTransferService(context)
        }
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
        // Phase 3D: Prevent duplicate entries for the same transfer_id
        if (current.any { it.transferId == entry.transferId }) {
            Log.d(TAG, "Skipping duplicate history entry for ${entry.transferId.take(8)}")
            return
        }
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
