# Ferry Phase 5: Notification Mirroring Implementation Report

## Summary
Successfully implemented Android -> Linux notification mirroring over the existing authenticated and encrypted local network socket. The user receives desktop notifications mapped accurately to their Android device events, minus any explicit app payloads/icons per spec constraints.

## Components Implemented

### Android
1. **FerryNotificationListenerService**: Bound to system Notification listener. Intercepts incoming messages, executes logic filter.
2. **NotificationFilter**: Rejects ongoing, non-clearable, group summaries, our own app, and transport media events.
3. **FerryIdGenerator**: Derives a stable 16-byte HMAC-SHA256 Base64-encoded `ferry_id` from the raw SBN key using a locally generated SharedPreferences salt.
4. **NotificationDeduplicator**: Content-based hashing (SHA-256 of Title+Body). Drops duplicate payloads for the same ID to prevent spam.
5. **NotificationDispatcher**: Manages a burst buffer (max 20) with TTL (60s). Routes payloads directly into the active `FerryControlClient` when `ESTABLISHED` and peer `peerSupportsNotify == true`.
6. **UI Integration**: Added Settings Switch in `FerryApp` mapping to `NotificationSettingsStore`, dynamically directing users to system Notification Access Settings if permission isn't granted.
7. **Pairing UI Fix**: Cleaned up the pairing dialog flow to display an inactive "Cancel" rather than "Reject" when in `PAIRING` state, while preserving proper `ACCEPT/REJECT` for `WAITING_FOR_LOCAL_DECISION`.

### Linux
1. **Models Extension**: Added `CAPABILITIES`, `NOTIFICATION_POST` and `NOTIFICATION_REMOVE` to IPC messaging payloads. Added tight parsing caps (`NOTIF_MAX_TITLE`, `MAX_NOTIFICATION_FRAME_BYTES`) to prevent malicious floods.
2. **NotificationBridge**: Synchronous GLib DBus client calling `org.freedesktop.Notifications`. 
   - Manages a Token Bucket rate limiter (10 burst, 1/sec sustained)
   - Escapes pango markup (`<`, `>`, `&`).
   - Tracks a bounded LRU map (200 items, 4h TTL) associating `ferry_id` -> `dbus_id`.
   - Listens to `NotificationClosed` signal for internal state maintenance.
3. **Service Wiring**: Handlers in `Session Loop` parse notification messages directly into the NotificationBridge instance, maintaining system integration. 

## Protocol Expansion
- **CAPABILITIES Exchange**: Initiated implicitly right after the `ESTABLISHED` transition. Currently sending/reading `notify.v1` as an arbitrary array item.

## Pending Verification
Physical test execution via `adb` and the GTK4 application must occur in the subsequent user iteration.
