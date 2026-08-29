package dev.ferry.app.discovery

import android.net.nsd.NsdServiceInfo
import dev.ferry.app.protocol.ProtocolConstants

/**
 * Represents a peer discovered on the local network (UNTRUSTED).
 */
data class DiscoveredDevice(
    val deviceId: String,
    val deviceName: String,
    val deviceType: String,
    val osName: String,
    val protocolVersion: Int,
    val host: String,
    val port: Int,
    val serviceName: String,
    val lastSeen: Long = System.currentTimeMillis(),
    val isAvailable: Boolean = true
) {
    companion object {
        fun fromAttributes(
            attributes: Map<String, ByteArray>?,
            hostAddress: String?,
            port: Int,
            serviceName: String?
        ): DiscoveredDevice? {
            if (attributes == null) return null

            fun getAttr(key: String): String? {
                val bytes = attributes[key] ?: return null
                return String(bytes, Charsets.UTF_8)
            }

            val protoVerStr = getAttr("v") ?: "1"
            val protoVer = protoVerStr.toIntOrNull() ?: return null

            // Only accept matching protocol versions
            if (protoVer != ProtocolConstants.PROTOCOL_VERSION) {
                return null
            }

            val deviceId = getAttr("id") ?: return null
            val deviceName = getAttr("name") ?: serviceName ?: "Unknown Ferry Device"
            val deviceType = getAttr("type") ?: "unknown"
            val osName = getAttr("os") ?: "unknown"

            if (hostAddress.isNullOrBlank() || hostAddress == "0.0.0.0" || port <= 0) return null

            return DiscoveredDevice(
                deviceId = deviceId,
                deviceName = deviceName,
                deviceType = deviceType,
                osName = osName,
                protocolVersion = protoVer,
                host = hostAddress,
                port = port,
                serviceName = serviceName ?: "",
                lastSeen = System.currentTimeMillis(),
                isAvailable = true
            )
        }

        fun fromNsdServiceInfo(serviceInfo: NsdServiceInfo): DiscoveredDevice? {
            return fromAttributes(
                attributes = serviceInfo.attributes,
                hostAddress = serviceInfo.host?.hostAddress,
                port = serviceInfo.port,
                serviceName = serviceInfo.serviceName
            )
        }
    }
}
