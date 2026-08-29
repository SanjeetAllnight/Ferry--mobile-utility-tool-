package dev.ferry.app.protocol

/**
 * Ferry Wire Protocol v1 Constants and Definitions.
 */
object ProtocolConstants {
    const val PROTOCOL_VERSION: Int = 1
    const val DEFAULT_PORT: Int = 53770
    const val MDNS_SERVICE_TYPE: String = "_ferry._tcp."
    val MAGIC_BYTES: ByteArray = byteArrayOf(0x46.toByte(), 0x59.toByte()) // "FY"

    object MessageTypes {
        const val HANDSHAKE_INIT = "HANDSHAKE_INIT"
        const val HANDSHAKE_RESPONSE = "HANDSHAKE_RESPONSE"
        const val PAIR_REQUEST = "PAIR_REQUEST"
        const val PAIR_CONFIRM = "PAIR_CONFIRM"

        const val TRANSFER_REQUEST = "TRANSFER_REQUEST"
        const val TRANSFER_ACCEPT = "TRANSFER_ACCEPT"
        const val TRANSFER_REJECT = "TRANSFER_REJECT"
        const val TRANSFER_PROGRESS = "TRANSFER_PROGRESS"
        const val TRANSFER_CANCEL = "TRANSFER_CANCEL"
        const val TRANSFER_COMPLETE = "TRANSFER_COMPLETE"
        const val TRANSFER_ERROR = "TRANSFER_ERROR"
    }

    object ErrorCodes {
        const val ERR_UNKNOWN_MESSAGE = "ERR_UNKNOWN_MESSAGE"
        const val ERR_UNAUTHORIZED = "ERR_UNAUTHORIZED"
        const val ERR_PAYLOAD_TOO_LARGE = "ERR_PAYLOAD_TOO_LARGE"
        const val ERR_INSUFFICIENT_STORAGE = "ERR_INSUFFICIENT_STORAGE"
        const val ERR_IO_FAILURE = "ERR_IO_FAILURE"
        const val ERR_INTEGRITY_MISMATCH = "ERR_INTEGRITY_MISMATCH"
        const val ERR_TIMEOUT = "ERR_TIMEOUT"
    }
}
