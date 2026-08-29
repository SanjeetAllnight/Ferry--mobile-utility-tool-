package dev.ferry.app

import dev.ferry.app.protocol.ProtocolConstants
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

class ProtocolConstantsTest {

    @Test
    fun testProtocolVersionAndPort() {
        assertEquals(1, ProtocolConstants.PROTOCOL_VERSION)
        assertEquals(53770, ProtocolConstants.DEFAULT_PORT)
        assertEquals("_ferry._tcp.", ProtocolConstants.MDNS_SERVICE_TYPE)
    }

    @Test
    fun testMagicBytes() {
        val magic = ProtocolConstants.MAGIC_BYTES
        assertEquals(2, magic.size)
        assertEquals(0x46.toByte(), magic[0]) // 'F'
        assertEquals(0x59.toByte(), magic[1]) // 'Y'
    }

    @Test
    fun testMessageTypesDefined() {
        assertNotNull(ProtocolConstants.MessageTypes.HANDSHAKE_INIT)
        assertNotNull(ProtocolConstants.MessageTypes.TRANSFER_REQUEST)
        assertNotNull(ProtocolConstants.MessageTypes.TRANSFER_ACCEPT)
        assertNotNull(ProtocolConstants.MessageTypes.TRANSFER_PROGRESS)
        assertNotNull(ProtocolConstants.MessageTypes.TRANSFER_COMPLETE)
    }
}
