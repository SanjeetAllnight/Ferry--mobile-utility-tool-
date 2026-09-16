package dev.ferry.app.ui

import android.content.Context
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.ferry.app.R
import dev.ferry.app.discovery.DiscoveredDevice
import dev.ferry.app.discovery.FerryDiscoveryEngine
import dev.ferry.app.net.FerryControlClient
import dev.ferry.app.protocol.ProtocolConstants
import dev.ferry.app.security.FerrySession
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FerryApp(
    discoveryEngine: FerryDiscoveryEngine? = null,
    controlClient: FerryControlClient? = null,
    sharedUri: Uri? = null,
    sharedUris: List<Uri> = emptyList(),
    onSharedUriHandled: () -> Unit = {},
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()

    val discoveredDevices by discoveryEngine?.discoveredDevices?.collectAsState()
        ?: remember { mutableStateOf(emptyList()) }

    val sessionState by controlClient?.sessionState?.collectAsState()
        ?: remember { mutableStateOf(FerrySession.State.DISCONNECTED) }

    val connectedDevice by controlClient?.connectedDevice?.collectAsState()
        ?: remember { mutableStateOf(null) }

    val sasCode by controlClient?.sasCode?.collectAsState()
        ?: remember { mutableStateOf(null) }

    val transferProgress by controlClient?.transferProgress?.collectAsState()
        ?: remember { mutableStateOf(null) }

    val incomingProgress by controlClient?.incomingTransferProgress?.collectAsState()
        ?: remember { mutableStateOf(null) }

    val transferHistory by controlClient?.transferHistory?.collectAsState()
        ?: remember { mutableStateOf(emptyList()) }

    val interruptedTransfers by controlClient?.interruptedTransfers?.collectAsState()
        ?: remember { mutableStateOf(emptyList()) }

    val remoteClipboard by controlClient?.remoteClipboard?.collectAsState()
        ?: remember { mutableStateOf(null) }

    // Clipboard sync: write remote clipboard to Android when it changes
    LaunchedEffect(remoteClipboard) {
        val text = remoteClipboard ?: return@LaunchedEffect
        val cm = context.getSystemService(android.content.ClipboardManager::class.java)
        cm?.setPrimaryClip(android.content.ClipData.newPlainText("Ferry Clipboard Sync", text))
    }

    val clipboardSyncEnabled = remember { mutableStateOf(false) }
    val isEstablished = sessionState == FerrySession.State.ESTABLISHED

    val multipleFilePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetMultipleContents()
    ) { uris: List<Uri> ->
        if (uris.isNotEmpty()) {
            scope.launch {
                val batchName = "Batch of ${uris.size} files"
                controlClient?.sendBatch(uris, context, batchName)
            }
        }
    }

    val folderPicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenDocumentTree()
    ) { uri: Uri? ->
        if (uri != null) {
            scope.launch {
                val treeNode = androidx.documentfile.provider.DocumentFile.fromTreeUri(context, uri)
                if (treeNode != null) {
                    val batchName = treeNode.name ?: "Folder"
                    val uris = mutableListOf<Uri>()
                    collectUrisRecursively(treeNode, uris)
                    if (uris.isNotEmpty()) {
                        controlClient?.sendBatch(uris, context, batchName)
                    }
                }
            }
        }
    }

    // Auto-send pending shared URI when connected
    LaunchedEffect(sharedUri, isEstablished) {
        if (sharedUri != null && isEstablished) {
            controlClient?.sendFile(sharedUri, context)
            onSharedUriHandled()
        }
    }

    // Auto-send multiple pending shared URIs as a batch when connected
    LaunchedEffect(sharedUris, isEstablished) {
        if (sharedUris.isNotEmpty() && isEstablished) {
            val batchName = "Shared ${sharedUris.size} files"
            controlClient?.sendBatch(sharedUris, context, batchName)
            onSharedUriHandled()
        }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            Column {
                TopAppBar(
                    title = {
                        Column(verticalArrangement = Arrangement.Center) {
                            Text(
                                "FERRY",
                                style = MaterialTheme.typography.labelLarge,
                                color = MaterialTheme.colorScheme.primary,
                                fontWeight = FontWeight.Bold
                            )
                        }
                    },
                    colors = TopAppBarDefaults.topAppBarColors(
                        containerColor = MaterialTheme.colorScheme.background,
                        titleContentColor = MaterialTheme.colorScheme.primary
                    )
                )
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            }
        }
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 16.dp),
            verticalArrangement = Arrangement.spacedBy(24.dp)
        ) {
            // ── Status Header Card ─────────────────────────────────────────
            Surface(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(4.dp),
                color = MaterialTheme.colorScheme.surfaceContainerLow,
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
            ) {
                Row(
                    modifier = Modifier.padding(16.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Box(
                            modifier = Modifier
                                .size(8.dp)
                                .clip(RoundedCornerShape(0.dp))
                                .background(if (isEstablished) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline)
                        )
                        Column {
                            val statusTitle = if (isEstablished) "Connected securely" else "Network scan active"
                            Text(
                                text = statusTitle,
                                style = MaterialTheme.typography.titleMedium,
                                color = MaterialTheme.colorScheme.primary
                            )
                            Text(
                                text = "LOCAL NETWORK • END-TO-END ENCRYPTED",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                }
            }

            // ── Active Primary Peer Card ─────────────────────────────────────────
            if (isEstablished && connectedDevice != null) {
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    shape = RoundedCornerShape(4.dp),
                    color = MaterialTheme.colorScheme.surfaceContainer,
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
                ) {
                    Column(modifier = Modifier.padding(16.dp)) {
                        Row(
                            horizontalArrangement = Arrangement.SpaceBetween,
                            modifier = Modifier.fillMaxWidth()
                        ) {
                            Text(
                                "TRUSTED PEER",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                            Text(
                                "AUTHORIZED",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary
                            )
                        }
                        Spacer(modifier = Modifier.height(12.dp))
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(12.dp)
                        ) {
                            Box(
                                modifier = Modifier
                                    .size(48.dp)
                                    .background(MaterialTheme.colorScheme.surfaceContainerHighest, RoundedCornerShape(4.dp))
                            )
                            Column {
                                Text(
                                    text = connectedDevice?.deviceName ?: "Unknown Device",
                                    style = MaterialTheme.typography.headlineMedium,
                                    color = MaterialTheme.colorScheme.primary
                                )
                                Text(
                                    text = "ONLINE",
                                    style = MaterialTheme.typography.labelMedium,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                        Spacer(modifier = Modifier.height(16.dp))
                        
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Button(
                                onClick = { multipleFilePicker.launch("*/*") },
                                modifier = Modifier.weight(1f).height(48.dp),
                                shape = RoundedCornerShape(4.dp),
                                colors = ButtonDefaults.buttonColors(
                                    containerColor = MaterialTheme.colorScheme.primary,
                                    contentColor = MaterialTheme.colorScheme.onPrimary
                                )
                            ) {
                                Text("SEND FILES", style = MaterialTheme.typography.labelLarge)
                            }
                        }
                    }
                }
            }

            // ── Outgoing Transfer Progress ─────────────────────────────────
            if (transferProgress != null) {
                TransferProgressCard(
                    progress = transferProgress!!,
                    label = "SENDING",
                    onCancel = { controlClient?.cancelOutgoingTransfer(transferProgress!!.transferId) },
                )
            }

            // ── Incoming Transfer Progress ─────────────────────────────────
            if (incomingProgress != null) {
                TransferProgressCard(
                    progress = incomingProgress!!,
                    label = "RECEIVING",
                    onCancel = { controlClient?.cancelIncomingTransfer(incomingProgress!!.transferId) },
                )
            }

            // ── Nearby Devices ─────────────────────────────────────────────
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = "DEVICES ON SUBNET (${discoveredDevices.size})",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Text(
                        text = "AUTO-DISCOVERY",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.primary
                    )
                }

                if (discoveredDevices.isEmpty() && !isEstablished) {
                    Surface(
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(4.dp),
                        color = MaterialTheme.colorScheme.surface,
                        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
                    ) {
                        Row(
                            modifier = Modifier.padding(16.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(12.dp)
                        ) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(20.dp),
                                strokeWidth = 2.dp,
                                color = MaterialTheme.colorScheme.primary
                            )
                            Column {
                                Text(
                                    text = "Searching Local Network…",
                                    style = MaterialTheme.typography.bodyMedium,
                                    color = MaterialTheme.colorScheme.primary
                                )
                                Text(
                                    text = "Make sure Ferry is running on your desktop on the same Wi-Fi.",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant
                                )
                            }
                        }
                    }
                } else {
                    discoveredDevices.forEach { device ->
                        DiscoveredDeviceCard(
                            device = device,
                            isConnected = connectedDevice?.deviceId == device.deviceId &&
                                    sessionState == FerrySession.State.ESTABLISHED,
                            isConnecting = connectedDevice?.deviceId == device.deviceId &&
                                    sessionState !in setOf(
                                        FerrySession.State.DISCONNECTED,
                                        FerrySession.State.ESTABLISHED,
                                        FerrySession.State.FAILED,
                                    ),
                            onConnect = {
                                if (sessionState == FerrySession.State.DISCONNECTED ||
                                    sessionState == FerrySession.State.FAILED
                                ) {
                                    controlClient?.connect(device)
                                }
                            },
                            onDisconnect = { controlClient?.disconnect() }
                        )
                    }
                }
            }

            // ── Interrupted Transfers (Resumable) ──────────────────────────
            if (interruptedTransfers.isNotEmpty()) {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        text = "INTERRUPTED TRANSFERS",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    interruptedTransfers.forEach { record ->
                        InterruptedTransferRow(
                            record = record,
                            onResume = { scope.launch { controlClient?.requestResume(record) } },
                            onDiscard = { controlClient?.discardInterrupted(record.transferId) }
                        )
                    }
                }
            }

            // ── Transfer History ───────────────────────────────────────────
            if (transferHistory.isNotEmpty()) {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        text = "TRANSFER HISTORY",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    transferHistory.take(10).forEach { entry ->
                        TransferHistoryRow(entry)
                    }
                }
            }

            // Clipboard Sync / Settings removed from main UI
        }

        // ── Pairing Dialog ─────────────────────────────────────────────────
        if (sessionState == FerrySession.State.PAIRING ||
            sessionState == FerrySession.State.WAITING_FOR_LOCAL_DECISION
        ) {
            AlertDialog(
                onDismissRequest = { controlClient?.rejectPairing() },
                containerColor = MaterialTheme.colorScheme.surfaceContainer,
                shape = RoundedCornerShape(0.dp),
                title = { Text("PAIR NEW DEVICE", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary) },
                text = {
                    Column {
                        Text("Verify this code matches the other device:", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(modifier = Modifier.height(16.dp))
                        Text(
                            text = sasCode ?: "…",
                            style = MaterialTheme.typography.displayLarge,
                            color = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.align(Alignment.CenterHorizontally)
                        )
                    }
                },
                confirmButton = {
                    Button(
                        onClick = { controlClient?.acceptPairing() },
                        shape = RoundedCornerShape(0.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = MaterialTheme.colorScheme.primary,
                            contentColor = MaterialTheme.colorScheme.onPrimary
                        )
                    ) { Text("ACCEPT", style = MaterialTheme.typography.labelLarge) }
                },
                dismissButton = {
                    Button(
                        onClick = { controlClient?.rejectPairing() },
                        shape = RoundedCornerShape(0.dp),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = Color.Transparent,
                            contentColor = MaterialTheme.colorScheme.error
                        ),
                        border = BorderStroke(1.dp, MaterialTheme.colorScheme.error)
                    ) { Text("REJECT", style = MaterialTheme.typography.labelLarge) }
                }
            )
        }
    }
}

// ── Transfer Progress Card ─────────────────────────────────────────────────────

@Composable
private fun TransferProgressCard(
    progress: FerryControlClient.TransferProgress,
    label: String,
    onCancel: (() -> Unit)? = null,
) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(4.dp),
        color = MaterialTheme.colorScheme.surfaceContainer,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "$label: ${progress.fileName}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.primary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f)
                )
                Text(
                    text = "${(progress.fraction * 100).toInt()}%",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(horizontal = 8.dp)
                )
            }
            LinearProgressIndicator(
                progress = { progress.fraction },
                modifier = Modifier.fillMaxWidth().height(4.dp),
                color = MaterialTheme.colorScheme.primary,
                trackColor = MaterialTheme.colorScheme.surfaceContainerHighest
            )
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "${formatBytes(progress.bytesDone)} / ${formatBytes(progress.totalBytes)}",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
                if (onCancel != null) {
                    TextButton(
                        onClick = onCancel,
                        contentPadding = PaddingValues(0.dp)
                    ) {
                        Text("CANCEL", color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.labelLarge)
                    }
                }
            }
        }
    }
}

// ── Interrupted Transfer Row ──────────────────────────────────────────────────

@Composable
private fun InterruptedTransferRow(
    record: dev.ferry.app.transfer.InterruptedTransferStore.InterruptedRecord,
    onResume: () -> Unit,
    onDiscard: () -> Unit,
) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(4.dp),
        color = MaterialTheme.colorScheme.surface,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
    ) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            val dirIcon = if (record.direction == dev.ferry.app.transfer.InterruptedTransferStore.InterruptedRecord.Direction.INCOMING) "↓" else "↑"
            Text(
                text = "$dirIcon  ${record.fileName}",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.primary,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis
            )

            val fraction = if (record.fileSize > 0) record.bytesReceived.toFloat() / record.fileSize else 0f
            LinearProgressIndicator(
                progress = { fraction },
                modifier = Modifier.fillMaxWidth().height(4.dp),
                color = MaterialTheme.colorScheme.outline,
                trackColor = MaterialTheme.colorScheme.surfaceContainerHighest
            )

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "${formatBytes(record.bytesReceived)} / ${formatBytes(record.fileSize)} (${(fraction * 100).toInt()}%)",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End)
            ) {
                Button(
                    onClick = onDiscard,
                    shape = RoundedCornerShape(4.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = Color.Transparent,
                        contentColor = MaterialTheme.colorScheme.error
                    ),
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.error),
                    modifier = Modifier.height(36.dp)
                ) {
                    Text("DISCARD", style = MaterialTheme.typography.labelLarge)
                }
                Button(
                    onClick = onResume,
                    shape = RoundedCornerShape(4.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary,
                        contentColor = MaterialTheme.colorScheme.onPrimary
                    ),
                    modifier = Modifier.height(36.dp)
                ) {
                    Text("RESUME", style = MaterialTheme.typography.labelLarge)
                }
            }
        }
    }
}

// ── Transfer History Row ───────────────────────────────────────────────────────

@Composable
private fun TransferHistoryRow(entry: FerryControlClient.TransferHistoryEntry) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(4.dp),
        color = MaterialTheme.colorScheme.surfaceContainerLow,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            val dirIcon = if (entry.direction == FerryControlClient.TransferProgress.Direction.INCOMING) "↓" else "↑"
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = "$dirIcon  ${entry.fileName}",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.primary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    text = formatBytes(entry.fileSize),
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Text(
                text = if (entry.success) "OK" else "FAIL",
                style = MaterialTheme.typography.labelLarge,
                color = if (entry.success) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.error,
            )
        }
    }
}

// ── Discovered Device Card ─────────────────────────────────────────────────────

@Composable
private fun DiscoveredDeviceCard(
    device: DiscoveredDevice,
    isConnected: Boolean = false,
    isConnecting: Boolean = false,
    onConnect: () -> Unit = {},
    onDisconnect: () -> Unit = {}
) {
    Surface(
        modifier = Modifier.fillMaxWidth().clickable { if (!isConnected && !isConnecting) onConnect() },
        shape = RoundedCornerShape(4.dp),
        color = MaterialTheme.colorScheme.surfaceContainer,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)
    ) {
        Row(
            modifier = Modifier.padding(16.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(16.dp),
                modifier = Modifier.weight(1f)
            ) {
                Box(
                    modifier = Modifier
                        .size(36.dp)
                        .background(MaterialTheme.colorScheme.surfaceContainerHighest, RoundedCornerShape(4.dp))
                )
                Column {
                    Text(
                        text = device.deviceName,
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.primary,
                        fontWeight = FontWeight.Medium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                    Text(
                        text = "NEARBY",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
            if (isConnecting) {
                Text("PAIRING...", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
            } else if (isConnected) {
                Button(
                    onClick = onDisconnect,
                    shape = RoundedCornerShape(4.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.surfaceContainerHighest,
                        contentColor = MaterialTheme.colorScheme.primary
                    ),
                    contentPadding = PaddingValues(horizontal = 16.dp, vertical = 0.dp),
                    modifier = Modifier.height(32.dp)
                ) {
                    Text("DISCONNECT", style = MaterialTheme.typography.labelLarge)
                }
            } else {
                Button(
                    onClick = onConnect,
                    shape = RoundedCornerShape(4.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary,
                        contentColor = MaterialTheme.colorScheme.onPrimary
                    ),
                    contentPadding = PaddingValues(horizontal = 16.dp, vertical = 0.dp),
                    modifier = Modifier.height(32.dp)
                ) {
                    Text("PAIR", style = MaterialTheme.typography.labelLarge)
                }
            }
        }
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

private fun formatBytes(bytes: Long): String {
    if (bytes <= 0) return "0 B"
    val units = arrayOf("B", "KB", "MB", "GB")
    var value = bytes.toDouble()
    var unit = 0
    while (value >= 1024 && unit < units.lastIndex) {
        value /= 1024
        unit++
    }
    return "%.1f %s".format(value, units[unit])
}

private fun collectUrisRecursively(
    node: androidx.documentfile.provider.DocumentFile,
    out: MutableList<Uri>,
) {
    for (child in node.listFiles()) {
        when {
            child.isDirectory -> collectUrisRecursively(child, out)
            child.isFile -> out.add(child.uri)
        }
    }
}
