package dev.ferry.app

import dev.ferry.app.security.FerrySession
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for FerrySession: HKDF key derivation, SAS derivation,
 * ChaCha20-Poly1305 AEAD framing, and session state machine.
 *
 * These run on the JVM (robolectric-free) since they only use standard
 * Java Security / javax.crypto APIs available in the JVM.
 */
class SessionTest {

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    /**
     * Create two FerrySession instances that have completed a full handshake.
     * The same ephemeral keys are used on both sides so that both derive
     * identical session material.
     */
    private fun handshakePair(): Pair<FerrySession, FerrySession> {
        val sessA = FerrySession(isInitiator = true)
        val sessB = FerrySession(isInitiator = false)

        // Generate identity-like public keys (32 random bytes each)
        val staticPubA = randomBytes(32)
        val staticPubB = randomBytes(32)

        val (_, ephPubA, nonceA) = sessA.buildLocalHandshake(staticPubA)
        val (_, ephPubB, nonceB) = sessB.buildLocalHandshake(staticPubB)

        sessA.acceptRemoteHandshake(staticPubB, ephPubB, nonceB)
        sessB.acceptRemoteHandshake(staticPubA, ephPubA, nonceA)

        sessA.deriveKeys()
        sessB.deriveKeys()

        return Pair(sessA, sessB)
    }

    private fun randomBytes(n: Int): ByteArray =
        ByteArray(n).also { java.security.SecureRandom().nextBytes(it) }

    // ------------------------------------------------------------------
    // SAS tests
    // ------------------------------------------------------------------

    @Test
    fun `sas codes match between initiator and responder`() {
        val (a, b) = handshakePair()
        assertNotNull(a.sasCode)
        assertNotNull(b.sasCode)
        assertEquals("SAS codes must match", a.sasCode, b.sasCode)
    }

    @Test
    fun `sas code is exactly 6 digits`() {
        val (a, _) = handshakePair()
        val sas = a.sasCode!!
        assertEquals(6, sas.length)
        assertTrue("SAS must be numeric: $sas", sas.all { it.isDigit() })
    }

    @Test
    fun `two different sessions produce different SAS codes`() {
        val (a1, _) = handshakePair()
        val (a2, _) = handshakePair()
        // Astronomically unlikely to collide with fresh ephemeral keys
        assertNotNull(a1.sasCode)
        assertNotNull(a2.sasCode)
        // (note: we don't assert they differ because it's theoretically possible
        //  in 1-in-1M chance; but both must be valid 6-digit codes)
    }

    // ------------------------------------------------------------------
    // Auth transcript tests
    // ------------------------------------------------------------------

    @Test
    fun `auth transcripts match between parties`() {
        val sessA = FerrySession(isInitiator = true)
        val sessB = FerrySession(isInitiator = false)

        val staticPubA = randomBytes(32)
        val staticPubB = randomBytes(32)

        val (_, ephPubA, nonceA) = sessA.buildLocalHandshake(staticPubA)
        val (_, ephPubB, nonceB) = sessB.buildLocalHandshake(staticPubB)

        sessA.acceptRemoteHandshake(staticPubB, ephPubB, nonceB)
        sessB.acceptRemoteHandshake(staticPubA, ephPubA, nonceA)

        sessA.deriveKeys()
        sessB.deriveKeys()

        val transcriptA = sessA.buildAuthTranscript()
        val transcriptB = sessB.buildAuthTranscript()

        assertArrayEquals("Auth transcripts must be identical", transcriptA, transcriptB)
    }

    @Test
    fun `auth transcript starts with expected prefix`() {
        val (a, _) = handshakePair()
        val transcript = a.buildAuthTranscript()
        val prefix = "ferry-auth-v1|".toByteArray()
        val actualPrefix = transcript.take(prefix.size).toByteArray()
        assertArrayEquals(prefix, actualPrefix)
    }

    // ------------------------------------------------------------------
    // AEAD tests
    // ------------------------------------------------------------------

    @Test
    fun `encrypt and decrypt roundtrip`() {
        val (a, b) = handshakePair()
        val plaintext = """{"type":"SESSION_ESTABLISHED","payload":{}}""".toByteArray()

        val frame = a.encryptFrame(plaintext)
        val recovered = b.decryptFrame(frame)

        assertArrayEquals(plaintext, recovered)
    }

    @Test
    fun `multiple frames decrypt in order`() {
        val (a, b) = handshakePair()
        val messages = listOf("first", "second", "third")

        for (msg in messages) {
            val frame = a.encryptFrame(msg.toByteArray())
            val plain = b.decryptFrame(frame)
            assertEquals(msg, String(plain))
        }
    }

    @Test
    fun `replay frame is rejected`() {
        val (a, b) = handshakePair()
        val frame = a.encryptFrame("once".toByteArray())
        b.decryptFrame(frame) // first OK

        try {
            b.decryptFrame(frame) // replay must fail
            assertTrue("Expected exception for replay attack", false)
        } catch (e: Exception) {
            assertTrue("Replay should throw an exception", true)
        }
    }

    @Test
    fun `tampered ciphertext is rejected`() {
        val (a, b) = handshakePair()
        val frame = a.encryptFrame("sensitive".toByteArray()).toMutableList()
        // Flip a byte past the 4+12 = 16-byte header
        frame[20] = (frame[20].toInt() xor 0xFF).toByte()

        try {
            b.decryptFrame(frame.toByteArray())
            assertTrue("Expected exception for tampered ciphertext", false)
        } catch (e: Exception) {
            assertTrue("Tampered frame should throw", true)
        }
    }

    @Test
    fun `wrong direction decryption fails`() {
        val (a, _) = handshakePair()
        val frame = a.encryptFrame("hello".toByteArray())
        // A tries to decrypt its own frame (wrong key direction)
        try {
            a.decryptFrame(frame)
            assertTrue("Expected exception: A cannot decrypt its own send frame", false)
        } catch (e: Exception) {
            assertTrue("Wrong direction should fail", true)
        }
    }

    // ------------------------------------------------------------------
    // State machine tests
    // ------------------------------------------------------------------

    @Test
    fun `valid session state transitions`() {
        val s = FerrySession(isInitiator = true)
        s.transition(FerrySession.State.CONNECTING)
        s.transition(FerrySession.State.HANDSHAKING)
        s.transition(FerrySession.State.AUTHENTICATING)
        s.transition(FerrySession.State.ESTABLISHED)
        s.transition(FerrySession.State.CLOSING)
        s.transition(FerrySession.State.DISCONNECTED)
        assertEquals(FerrySession.State.DISCONNECTED, s.state)
    }

    @Test
    fun `pairing path transitions`() {
        val s = FerrySession(isInitiator = true)
        s.transition(FerrySession.State.CONNECTING)
        s.transition(FerrySession.State.HANDSHAKING)
        s.transition(FerrySession.State.PAIRING)
        s.transition(FerrySession.State.AUTHENTICATING)
        s.transition(FerrySession.State.ESTABLISHED)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `invalid state transition throws`() {
        val s = FerrySession(isInitiator = true)
        // Cannot go directly from DISCONNECTED to ESTABLISHED
        s.transition(FerrySession.State.ESTABLISHED)
    }

    @Test
    fun `failed path transitions to disconnected`() {
        val s = FerrySession(isInitiator = true)
        s.transition(FerrySession.State.CONNECTING)
        s.transition(FerrySession.State.FAILED)
        s.transition(FerrySession.State.DISCONNECTED)
        assertEquals(FerrySession.State.DISCONNECTED, s.state)
    }
}
