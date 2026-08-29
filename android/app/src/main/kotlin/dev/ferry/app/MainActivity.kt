package dev.ferry.app

import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import dev.ferry.app.ui.FerryApp
import dev.ferry.app.ui.theme.FerryTheme

class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "FerryApp"
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()

        Log.i(TAG, "Ferry MainActivity started. Phase 1 foundation initialized.")

        setContent {
            FerryTheme {
                FerryApp()
            }
        }
    }
}
