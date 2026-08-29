# Ferry Wire Protocol Specification (v1)

This specification defines the communication format, framing, message types, and state machine for **Ferry Protocol Version 1 (`dev.ferry.v1`)**.

---

## 1. Framing & Wire Format

All Ferry Control Plane messages are transmitted over TCP with a binary framing header followed by a JSON payload.

### Frame Structure

```
+-------------------+-------------------+-----------------------------------------+
| Magic (2 Bytes)   | Length (4 Bytes)  | Payload (Length Bytes)                  |
| 0x46 0x59 ('FY')  | Big-Endian uint32 | UTF-8 JSON Encoded Message Envelope     |
+-------------------+-------------------+-----------------------------------------+
```

1. **Magic Bytes (2 bytes)**: Fixed prefix `0x46 0x59` (`ASCII "FY"`). Frames without this magic must be dropped immediately.
2. **Length (4 bytes)**: 32-bit unsigned integer in network byte order (big-endian), representing the byte count of the JSON payload. Maximum control frame payload size is `1,048,576` bytes (1 MiB).
3. **Payload**: UTF-8 encoded JSON string matching the standard message envelope.

---

## 2. Standard Message Envelope

Every control message uses the following top-level JSON structure:

```json
{
  "protocol_version": 1,
  "message_id": "c1f7a4e2-628d-4b92-8098-3a876a17b019",
  "reply_to": null,
  "timestamp": 1724932800123,
  "type": "MESSAGE_TYPE_STRING",
  "payload": {}
}
```

### Fields:
* `protocol_version` (*integer, mandatory*): Must be `1` for this specification.
* `message_id` (*string, UUIDv4, mandatory*): Unique identifier for this message.
* `reply_to` (*string, UUIDv4, optional*): The `message_id` this message is responding to.
* `timestamp` (*integer, mandatory*): Milliseconds since UNIX epoch (UTC).
* `type` (*string, mandatory*): Specific message type identifier.
* `payload` (*object, mandatory*): Type-specific JSON object.

---

## 3. Message Types

### 3.1. Discovery (mDNS / DNS-SD)

* **Service Type**: `_ferry._tcp.local.`
* **Default Port**: `53770`
* **TXT Records**:
  * `v=1` (Protocol version)
  * `id=<base64_device_public_id>`
  * `name=<display_name>`
  * `device_type=desktop|mobile`
  * `os=archlinux|android`

---

### 3.2. Session & Pairing Messages

#### `HANDSHAKE_INIT`
Initiates connection and exchanges cryptographic ephemeral parameters.
```json
{
  "type": "HANDSHAKE_INIT",
  "payload": {
    "device_id": "d8e8fca2-8567-4a51-9c88-21d96be7f521",
    "device_name": "Sanjeet Arch Desktop",
    "device_type": "desktop",
    "public_key": "base64_encoded_ed25519_public_key",
    "ephemeral_key": "base64_encoded_x25519_ephemeral_key",
    "is_paired": false
  }
}
```

#### `HANDSHAKE_RESPONSE`
Responder's reply with ephemeral parameters and authentication status.
```json
{
  "type": "HANDSHAKE_RESPONSE",
  "payload": {
    "device_id": "e3a890b1-1244-48ff-98ba-d57211100234",
    "device_name": "Realme RMX3870",
    "device_type": "mobile",
    "public_key": "base64_encoded_ed25519_public_key",
    "ephemeral_key": "base64_encoded_x25519_ephemeral_key",
    "is_paired": false
  }
}
```

#### `PAIR_REQUEST`
Requests out-of-band user confirmation.
```json
{
  "type": "PAIR_REQUEST",
  "payload": {
    "sas_code": "482 910",
    "timeout_seconds": 60
  }
}
```

#### `PAIR_CONFIRM`
Signals local user acceptance of the SAS verification PIN.
```json
{
  "type": "PAIR_CONFIRM",
  "payload": {
    "status": "ACCEPTED",
    "signature": "base64_signature_over_handshake_transcript"
  }
}
```

---

### 3.3. File Transfer Messages

#### `TRANSFER_REQUEST`
Sender requests permission to transmit one or more files.
```json
{
  "type": "TRANSFER_REQUEST",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "items": [
      {
        "item_id": "item-1",
        "file_name": "document.pdf",
        "file_size": 2458920,
        "mime_type": "application/pdf",
        "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      }
    ],
    "total_bytes": 2458920
  }
}
```

#### `TRANSFER_ACCEPT`
Receiver accepts the transfer request and specifies data port/channel.
```json
{
  "type": "TRANSFER_ACCEPT",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "accepted_items": ["item-1"],
    "data_stream_port": 53771
  }
}
```

#### `TRANSFER_REJECT`
Receiver rejects the transfer request.
```json
{
  "type": "TRANSFER_REJECT",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "reason": "USER_REJECTED"
  }
}
```

#### `TRANSFER_PROGRESS`
Periodically updates the peer on transmission status.
```json
{
  "type": "TRANSFER_PROGRESS",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "item_id": "item-1",
    "bytes_transferred": 1048576,
    "total_bytes": 2458920,
    "speed_bytes_per_sec": 52428800
  }
}
```

#### `TRANSFER_CANCEL`
Either peer cancels an active or queued transfer.
```json
{
  "type": "TRANSFER_CANCEL",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "reason": "USER_CANCELLED"
  }
}
```

#### `TRANSFER_COMPLETE`
Receiver confirms all data was received and verified via SHA-256 digest.
```json
{
  "type": "TRANSFER_COMPLETE",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "item_id": "item-1",
    "status": "VERIFIED",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  }
}
```

#### `TRANSFER_ERROR`
Sent when a fatal stream error or integrity failure occurs.
```json
{
  "type": "TRANSFER_ERROR",
  "payload": {
    "transfer_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "error_code": "INTEGRITY_MISMATCH",
    "message": "Calculated SHA-256 digest does not match expected metadata."
  }
}
```

---

## 4. Transfer State Machine

```
                    ┌──────────────┐
                    │     IDLE     │
                    └──────┬───────┘
                           │ Send/Receive TRANSFER_REQUEST
                           ▼
                    ┌──────────────┐
            ┌───────┤   PENDING    ├────────┐
            │       └──────┬───────┘        │
 User Reject│              │ User Accept    │ Timeout / Cancel
            ▼              ▼                ▼
     ┌────────────┐ ┌──────────────┐ ┌─────────────┐
     │  REJECTED  │ │ IN_PROGRESS  │ │  CANCELLED  │
     └────────────┘ └──────┬───────┘ └─────────────┘
                           │
                 Stream Data & Progress
                           │
              ┌────────────┴────────────┐
              │                         │
     Checksum Verified        Checksum Mismatch / Error
              ▼                         ▼
       ┌─────────────┐           ┌─────────────┐
       │  COMPLETED  │           │   FAILED    │
       └─────────────┘           └─────────────┘
```

---

## 5. Standard Error Codes

* `ERR_UNKNOWN_MESSAGE`: Message type not supported in this protocol version.
* `ERR_UNAUTHORIZED`: Attempted transfer on an unpaired or unauthenticated connection.
* `ERR_PAYLOAD_TOO_LARGE`: Frame payload exceeded maximum 1 MiB limit.
* `ERR_INSUFFICIENT_STORAGE`: Destination filesystem does not have enough free space.
* `ERR_IO_FAILURE`: Disk write or read failure.
* `ERR_INTEGRITY_MISMATCH`: Final SHA-256 verification failed.
* `ERR_TIMEOUT`: Peer failed to respond within required window.
