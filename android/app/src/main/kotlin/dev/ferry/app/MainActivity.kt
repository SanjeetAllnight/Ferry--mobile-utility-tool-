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
import dev.ferry.app.notification.FerryNotificationListenerService
import dev.ferry.app.notification.NotificationDispatcher
import android.content.Intent
import android.net.Uri
import androidx.compose.runtime.mutableStateOf

class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "FerryApp"
    }

    private lateinit var discoveryEngine: FerryDiscoveryEngine
    private lateinit var identity: FerryIdentity
    private lateinit var trustStore: FerryTrustStore
    private lateinit var controlClient: FerryControlClient

    private val sharedUri = mutableStateOf<Uri?>(null)
    private val sharedUris = mutableStateOf<List<Uri>>(emptyList())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        identity = FerryIdentity(applicationContext)
        trustStore = FerryTrustStore(applicationContext)
        controlClient = FerryControlClient(identity, trustStore, applicationContext)
        discoveryEngine = FerryDiscoveryEngine(applicationContext)
        
        // Phase 5: Inject notification dispatcher
        FerryNotificationListenerService.dispatcher = NotificationDispatcher(controlClient)

        Log.i(
            TAG,
            "Ferry started. DeviceID=${discoveryEngine.deviceId}, " +
                "IdentityKey=${identity.publicKeyB64.take(12)}..."
        )

        handleIntent(intent)

        setContent {
            FerryTheme {
                FerryApp(
                    discoveryEngine = discoveryEngine,
                    controlClient = controlClient,
                    sharedUri = sharedUri.value,
                    sharedUris = sharedUris.value,
                    onSharedUriHandled = {
                        sharedUri.value = null
                        sharedUris.value = emptyList()
                    }
                )
            }
        }
    }

    override fun onStart() {
        super.onStart()
        discoveryEngine.start()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (isFinishing) {
            discoveryEngine.stop()
            controlClient.disconnect()
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIntent(intent)
    }

    @Suppress("DEPRECATION")
    private fun handleIntent(intent: Intent?) {
        when (intent?.action) {
            Intent.ACTION_SEND -> {
                val uri = intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
                if (uri != null) {
                    Log.i(TAG, "Received ACTION_SEND with URI: $uri")
                    sharedUri.value = uri
                    sharedUris.value = emptyList()
                }
            }
            Intent.ACTION_SEND_MULTIPLE -> {
                val uris = intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
                if (!uris.isNullOrEmpty()) {
                    Log.i(TAG, "Received ACTION_SEND_MULTIPLE with ${uris.size} URIs")
                    sharedUris.value = uris
                    sharedUri.value = null
                }
            }
        }
    }
}
