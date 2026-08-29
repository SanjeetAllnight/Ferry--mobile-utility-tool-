package dev.ferry.app

import dev.ferry.app.discovery.DiscoveredDevice
import dev.ferry.app.protocol.ProtocolConstants
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DiscoveredDeviceTest {

    @Test
    fun testDiscoveredDeviceCreation() {
        val device = DiscoveredDevice(
            deviceId = "test-arch-desktop-id",
            deviceName = "Arch Linux Desktop",
            deviceType = "desktop",
            osName = "archlinux",
            protocolVersion = 1,
            host = "192.168.1.100",
            port = 53770,
            serviceName = "Ferry-test._ferry._tcp",
            isAvailable = true
        )

        assertEquals("test-arch-desktop-id", device.deviceId)
        assertEquals("Arch Linux Desktop", device.deviceName)
        assertEquals("desktop", device.deviceType)
        assertEquals("archlinux", device.osName)
        assertEquals(1, device.protocolVersion)
        assertEquals("192.168.1.100", device.host)
        assertEquals(53770, device.port)
        assertTrue(device.isAvailable)
    }

    @Test
    fun testDiscoveredDeviceFromAttributes() {
        val attributes = mapOf(
            "v" to "1".toByteArray(Charsets.UTF_8),
            "id" to "desktop-uuid-456".toByteArray(Charsets.UTF_8),
            "name" to "Office Workstation".toByteArray(Charsets.UTF_8),
            "type" to "desktop".toByteArray(Charsets.UTF_8),
            "os" to "archlinux".toByteArray(Charsets.UTF_8),
            "port" to "53770".toByteArray(Charsets.UTF_8)
        )

        val device = DiscoveredDevice.fromAttributes(
            attributes = attributes,
            hostAddress = "192.168.1.105",
            port = 53770,
            serviceName = "Ferry-desktop123"
        )

        assertNotNull(device)
        assertEquals("desktop-uuid-456", device?.deviceId)
        assertEquals("Office Workstation", device?.deviceName)
        assertEquals("desktop", device?.deviceType)
        assertEquals("archlinux", device?.osName)
        assertEquals(ProtocolConstants.PROTOCOL_VERSION, device?.protocolVersion)
        assertEquals("192.168.1.105", device?.host)
        assertEquals(53770, device?.port)
    }

    @Test
    fun testRejectUnsupportedProtocolVersion() {
        val attributes = mapOf(
            "v" to "99".toByteArray(Charsets.UTF_8),
            "id" to "desktop-uuid-999".toByteArray(Charsets.UTF_8),
            "name" to "Future Device".toByteArray(Charsets.UTF_8)
        )

        val device = DiscoveredDevice.fromAttributes(
            attributes = attributes,
            hostAddress = "192.168.1.105",
            port = 53770,
            serviceName = "Ferry-future"
        )
        assertNull("Should reject protocol version 99", device)
    }

    @Test
    fun testRejectMissingDeviceId() {
        val attributes = mapOf(
            "v" to "1".toByteArray(Charsets.UTF_8),
            "name" to "No ID Device".toByteArray(Charsets.UTF_8)
        )

        val device = DiscoveredDevice.fromAttributes(
            attributes = attributes,
            hostAddress = "192.168.1.105",
            port = 53770,
            serviceName = "Ferry-noid"
        )
        assertNull("Should reject when id attribute is missing", device)
    }

    @Test
    fun testRejectInvalidHostOrPort() {
        val attributes = mapOf(
            "v" to "1".toByteArray(Charsets.UTF_8),
            "id" to "valid-id".toByteArray(Charsets.UTF_8)
        )

        val deviceNullHost = DiscoveredDevice.fromAttributes(
            attributes = attributes,
            hostAddress = null,
            port = 53770,
            serviceName = "Ferry-test"
        )
        assertNull("Should reject null host", deviceNullHost)

        val deviceInvalidPort = DiscoveredDevice.fromAttributes(
            attributes = attributes,
            hostAddress = "192.168.1.100",
            port = 0,
            serviceName = "Ferry-test"
        )
        assertNull("Should reject port 0", deviceInvalidPort)
    }
}
