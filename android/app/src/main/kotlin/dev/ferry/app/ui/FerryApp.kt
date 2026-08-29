package dev.ferry.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CenterAlignedTopAppBar
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.ferry.app.R
import dev.ferry.app.discovery.DiscoveredDevice
import dev.ferry.app.discovery.FerryDiscoveryEngine
import dev.ferry.app.net.FerryControlClient
import dev.ferry.app.protocol.ProtocolConstants
import dev.ferry.app.security.FerrySession

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FerryApp(
    discoveryEngine: FerryDiscoveryEngine? = null,
    controlClient: FerryControlClient? = null,
) {
    val discoveredDevices by discoveryEngine?.discoveredDevices?.collectAsState()
        ?: remember { mutableStateOf(emptyList()) }

    val sessionState by controlClient?.sessionState?.collectAsState()
        ?: remember { mutableStateOf(FerrySession.State.DISCONNECTED) }

    val connectedDevice by controlClient?.connectedDevice?.collectAsState()
        ?: remember { mutableStateOf(null) }

    val sasCode by controlClient?.sasCode?.collectAsState()
        ?: remember { mutableStateOf(null) }

    Scaffold(
        topBar = {
            CenterAlignedTopAppBar(
                title = {
                    Text(
                        text = stringResource(R.string.app_name),
                        fontWeight = FontWeight.Bold,
                        style = MaterialTheme.typography.titleLarge
                    )
                },
                colors = TopAppBarDefaults.centerAlignedTopAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface
                )
            )
        }
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)
        ) {
            // Status Header Card
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(20.dp),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.primaryContainer
                )
            ) {
                Column(
                    modifier = Modifier.padding(20.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(8.dp)
                    ) {
                        val isEstablished = sessionState == FerrySession.State.ESTABLISHED
                        Box(
                            modifier = Modifier
                                .size(10.dp)
                                .clip(CircleShape)
                                .background(
                                    if (isEstablished) Color(0xFF2E7D32) else Color(0xFF1565C0)
                                )
                        )
                        Text(
                            text = if (isEstablished)
                                "Phase 2B: Secure Session Active"
                            else
                                "Phase 2B: Control Plane Ready",
                            style = MaterialTheme.typography.labelSmall,
                            fontWeight = FontWeight.Bold,
                            color = MaterialTheme.colorScheme.onPrimaryContainer
                        )
                    }

                    Text(
                        text = "Android ↔ Arch Linux",
                        style = MaterialTheme.typography.headlineLarge,
                        color = MaterialTheme.colorScheme.onPrimaryContainer
                    )

                    val statusText = when (sessionState) {
                        FerrySession.State.DISCONNECTED -> "mDNS peer discovery active on local Wi-Fi (_ferry._tcp)."
                        FerrySession.State.CONNECTING -> "Connecting to ${connectedDevice?.deviceName}..."
                        FerrySession.State.HANDSHAKING -> "Performing cryptographic handshake..."
                        FerrySession.State.PAIRING -> "Pairing — verify code with peer"
                        FerrySession.State.WAITING_FOR_LOCAL_DECISION -> "Pairing — waiting for your approval"
                        FerrySession.State.WAITING_FOR_REMOTE_DECISION -> "Waiting for peer to accept..."
                        FerrySession.State.PAIR_ACCEPTED -> "Pairing mutually accepted!"
                        FerrySession.State.AUTHENTICATING -> "Authenticating with ${connectedDevice?.deviceName}..."
                        FerrySession.State.ESTABLISHED -> "Encrypted session with ${connectedDevice?.deviceName}."
                        FerrySession.State.CLOSING -> "Closing session..."
                        FerrySession.State.FAILED -> "Session failed. Tap a device to retry."
                    }
                    Text(
                        text = statusText,
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.8f)
                    )
                }
            }

            // Engine & Protocol Details Card
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.4f)
                )
            ) {
                Column(
                    modifier = Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)
                ) {
                    Text(
                        text = "Discovery Status",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold
                    )

                    InfoRow(
                        label = "Local Device Name",
                        value = discoveryEngine?.deviceName ?: "Android (Ferry)"
                    )
                    InfoRow(
                        label = "Local Device ID",
                        value = discoveryEngine?.deviceId?.take(13)?.plus("...") ?: "Pending"
                    )
                    InfoRow(
                        label = "Service Type",
                        value = "_ferry._tcp (DNS-SD)"
                    )
                    InfoRow(
                        label = "Wire Protocol",
                        value = "dev.ferry.v${ProtocolConstants.PROTOCOL_VERSION}"
                    )
                }
            }

            // Discovered Nearby Devices Section
            Card(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(16.dp),
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.4f)
                )
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
                            text = "Nearby Devices (Untrusted)",
                            style = MaterialTheme.typography.titleMedium,
                            fontWeight = FontWeight.SemiBold
                        )
                        if (discoveredDevices.isNotEmpty()) {
                            Text(
                                text = "${discoveredDevices.size} found",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary
                            )
                        }
                    }

                    if (discoveredDevices.isEmpty()) {
                        Surface(
                            modifier = Modifier.fillMaxWidth(),
                            shape = RoundedCornerShape(12.dp),
                            color = MaterialTheme.colorScheme.surface
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
                                        text = "Searching Local Network...",
                                        style = MaterialTheme.typography.bodyMedium,
                                        fontWeight = FontWeight.Medium
                                    )
                                    Text(
                                        text = "Make sure Ferry is running on your Arch Linux desktop on the same Wi-Fi.",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.6f)
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
                                onDisconnect = { controlClient?.disconnect() },
                            )
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            if (sessionState == FerrySession.State.PAIRING || sessionState == FerrySession.State.WAITING_FOR_LOCAL_DECISION) {
                AlertDialog(
                    onDismissRequest = { controlClient?.rejectPairing() },
                    title = { Text("Pairing Request") },
                    text = {
                        Column {
                            Text("Does this code match the one on the other device?")
                            Spacer(modifier = Modifier.height(16.dp))
                            Text(
                                text = sasCode ?: "...",
                                style = MaterialTheme.typography.displayMedium,
                                fontWeight = FontWeight.Bold,
                                modifier = Modifier.align(Alignment.CenterHorizontally)
                            )
                        }
                    },
                    confirmButton = {
                        Button(onClick = { controlClient?.acceptPairing() }) {
                            Text("Accept")
                        }
                    },
                    dismissButton = {
                        TextButton(onClick = { controlClient?.rejectPairing() }) {
                            Text("Reject", color = MaterialTheme.colorScheme.error)
                        }
                    }
                )
            }
        }
    }
}

@Composable
private fun DiscoveredDeviceCard(
    device: DiscoveredDevice,
    isConnected: Boolean = false,
    isConnecting: Boolean = false,
    onConnect: () -> Unit = {},
    onDisconnect: () -> Unit = {},
) {
    Surface(
        modifier = Modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surface
    ) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = if (device.deviceType == "desktop") "💻 ${device.deviceName}" else "📱 ${device.deviceName}",
                    style = MaterialTheme.typography.bodyLarge,
                    fontWeight = FontWeight.Bold
                )
                val (badgeText, badgeColor, badgeBg) = when {
                    isConnected -> Triple("● Secure", Color(0xFF1B5E20), Color(0xFFE8F5E9))
                    isConnecting -> Triple("Pairing…", Color(0xFF0D47A1), Color(0xFFE3F2FD))
                    else -> Triple("Available", MaterialTheme.colorScheme.onPrimaryContainer, MaterialTheme.colorScheme.primaryContainer)
                }
                Surface(
                    shape = RoundedCornerShape(6.dp),
                    color = badgeBg
                ) {
                    Text(
                        text = badgeText,
                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp),
                        style = MaterialTheme.typography.labelSmall,
                        fontWeight = FontWeight.Bold,
                        color = badgeColor
                    )
                }
            }

            Text(
                text = "${device.host}:${device.port} • OS: ${device.osName.replaceFirstChar { it.uppercase() }} • Protocol v${device.protocolVersion}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f)
            )

            if (!isConnecting) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End
                ) {
                    if (isConnected) {
                        Button(
                            onClick = onDisconnect,
                            colors = ButtonDefaults.buttonColors(
                                containerColor = MaterialTheme.colorScheme.errorContainer,
                                contentColor = MaterialTheme.colorScheme.onErrorContainer,
                            ),
                        ) { Text("Disconnect") }
                    } else {
                        Button(onClick = onConnect) { Text("Connect") }
                    }
                }
            } else {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(16.dp),
                        strokeWidth = 2.dp,
                    )
                }
            }
        }
    }
}

@Composable
private fun InfoRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Text(
            text = value,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface
        )
    }
}
