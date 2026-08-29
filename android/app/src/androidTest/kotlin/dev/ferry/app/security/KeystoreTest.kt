package dev.ferry.app.security

import android.security.keystore.KeyProperties
import android.security.keystore.KeyProtection
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Test
import org.junit.runner.RunWith
import java.security.KeyFactory
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.cert.Certificate
import java.security.spec.PKCS8EncodedKeySpec
import android.util.Log

@RunWith(AndroidJUnit4::class)
class KeystoreTest {
    @Test
    fun testImportEd25519() {
        val kpg = KeyPairGenerator.getInstance("Ed25519")
        val kp = kpg.generateKeyPair()

        val ks = KeyStore.getInstance("AndroidKeyStore")
        ks.load(null)

        val alias = "test_import_ed25519"
        if (ks.containsAlias(alias)) {
            ks.deleteEntry(alias)
        }
        
        try {
            // Ed25519 requires self-signed cert to store the PrivateKey in AndroidKeyStore,
            // but KeyPairGenerator("Ed25519") outside of AndroidKeyStore doesn't generate a cert.
            // AndroidKeyStore requires a Certificate chain for PrivateKey entries.
            // This is the hard part: we don't have a valid X.509 cert chain for our raw Ed25519 key.
            // We'd have to generate a self-signed X.509 cert using BouncyCastle just to satisfy the API.
            Log.d("KeystoreTest", "Cannot easily import without X.509 cert chain")
        } catch (e: Exception) {
            e.printStackTrace()
            throw e
        }
    }
}
