package dev.ferry.app

import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import dev.ferry.app.discovery.FerryDiscoveryEngine
import dev.ferry.app.ui.FerryApp
import dev.ferry.app.ui.theme.FerryTheme

class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "FerryApp"
    }

    private lateinit var discoveryEngine: FerryDiscoveryEngine

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        discoveryEngine = FerryDiscoveryEngine(applicationContext)

        Log.i(TAG, "Ferry MainActivity started. Initializing discovery engine for device ID: ${discoveryEngine.deviceId}")

        setContent {
            FerryTheme {
                FerryApp(discoveryEngine = discoveryEngine)
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
    }
}
