# Ferry Product Scope

Ferry is a minimal, sovereign, and private Android ↔ Linux local communication bridge.

## Core Product Vision
The application intentionally acts as a small, focused utility rather than a heavy suite of device management tools. The interface and functionality are strictly limited to features that execute locally, securely, and reliably over the local network.

## Active Scope

Ferry has **ONLY TWO** user-facing feature areas:

### 1. Secure File Transfer (Implemented)
- Local network Auto-discovery (mDNS)
- Authenticated pairing (Ed25519 cryptography)
- File transfer from Android → Linux
- File transfer from Linux → Android
- Single file, multiple files, and folder/directory transfer
- Large file support
- Transfer progress and cancellation
- Background transfer operation
- Transfer history tracking
- Android Share Sheet integration
- **Failure Recovery:** Resumability for interrupted transfers (where currently supported).

### 2. Notifications (Next Milestone)
- Mirroring of important Android notifications to Linux.
- Display of incoming messages, calls, and other useful phone notifications on the desktop.

*Note: Notifications are read-only mirroring. Interactive messaging, call answering, SMS control, or remote interaction are strictly excluded.*

---

## Explicitly Deferred & Excluded Scope

The following features have been explicitly removed from the active product scope and are treated as deferred to a future, separate project phase (or abandoned entirely to maintain minimal product focus):

- Clipboard synchronization
- QR code or manual IP pairing fallbacks
- Network diagnostics and developer-centric connection details exposed in the UI
- Remote filesystem browsing
- Media controls
- Screen mirroring
- Remote desktop access
- Cloud synchronization or relay servers
- General device control
- SMS sending/receiving functionality
- Calling functionality
- Camera streaming
- Remote command execution
- Advanced Phone Access

**The final state of Ferry aims to communicate one clear purpose:**  
`Private local connection` → `Files + Notifications` → `Nothing more.`
