package dev.ferry.app

import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import dev.ferry.app.discovery.FerryDiscoveryEngine
import dev.ferry.app.net.FerryControlClient
import dev.ferry.app.net.FerryTrustStore
import dev.ferry.app.security.FerryIdentity
import dev.ferry.app.ui.FerryApp
import dev.ferry.app.ui.theme.FerryTheme

class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "FerryApp"
    }

    private lateinit var discoveryEngine: FerryDiscoveryEngine
    private lateinit var identity: FerryIdentity
    private lateinit var trustStore: FerryTrustStore
    private lateinit var controlClient: FerryControlClient

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        identity = FerryIdentity(applicationContext)
        trustStore = FerryTrustStore(applicationContext)
        controlClient = FerryControlClient(identity, trustStore)
        discoveryEngine = FerryDiscoveryEngine(applicationContext)

        Log.i(
            TAG,
            "Ferry started. DeviceID=${discoveryEngine.deviceId}, " +
                "IdentityKey=${identity.publicKeyB64.take(12)}..."
        )

        setContent {
            FerryTheme {
                FerryApp(
                    discoveryEngine = discoveryEngine,
                    controlClient = controlClient,
                )
            }
        }
    }

    override fun onStart() {
        super.onStart()
        discoveryEngine.start()
    }

    override fun onStop() {
        super.onStop()
        discoveryEngine.stop()
        controlClient.disconnect()
    }
}
