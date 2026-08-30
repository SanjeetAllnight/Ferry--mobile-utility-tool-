package dev.ferry.app.transfer

import android.content.Context
import android.util.Log
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.ferry.app.discovery.DiscoveredDevice
import dev.ferry.app.net.FerryControlClient
import dev.ferry.app.net.FerryTrustStore
import dev.ferry.app.security.FerryIdentity
import dev.ferry.app.security.FerrySession
import dev.ferry.app.protocol.ProtocolConstants
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.io.FileOutputStream
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class Phase3BPhysicalSenderTest {

    @Test
    fun sendTestFileToLinux() = runBlocking {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val identity = FerryIdentity(context)
        val trustStore = FerryTrustStore(context)
        val client = FerryControlClient(identity, trustStore)
        
        // Find the Linux device IP (hardcoded for test based on previous logs)
        val linuxDevice = DiscoveredDevice(
            deviceId = "bf347bfd-8a8e-4e95-9e3d-d03d37a07b31",
            deviceName = "archnoir (Ferry)",
            deviceType = "desktop",
            host = "10.213.207.51",
            port = 53770,
            serviceName = "Ferry-bf347bfd._ferry._tcp.local.",
            osName = "linux",
            protocolVersion = 1
        )
        
        Log.i("TestSender", "Connecting to Linux...")
        client.connect(linuxDevice)
        
        // Wait for connection to establish or pairing
        var attempts = 0
        while (client.sessionState.value != FerrySession.State.ESTABLISHED && attempts < 60) {
            if (client.sessionState.value == FerrySession.State.WAITING_FOR_LOCAL_DECISION || 
                client.sessionState.value == FerrySession.State.PAIRING) {
                Log.i("TestSender", "Auto-accepting pairing (state: ${client.sessionState.value})...")
                client.acceptPairing()
            }
            delay(1000)
            attempts++
        }
        
        if (client.sessionState.value != FerrySession.State.ESTABLISHED) {
            throw AssertionError("Failed to establish session. Current state: ${client.sessionState.value}")
        }
        
        Log.i("TestSender", "Session ESTABLISHED! Creating and sending test file...")
        
        // Create a 2.5 MB test file (byte sequence 0..255 repeating)
        val fileBytes = ByteArray(2_500_000) { (it % 256).toByte() }
        
        val success = client.sendFile(fileBytes, "test_android_to_linux.bin", "application/octet-stream")
        Log.i("TestSender", "File transfer result: $success")
        
        // Wait for a few seconds to let any final packets go through
        delay(5000)
        
        client.disconnect()
        assert(success) { "Failed to send file!" }
    }
}
