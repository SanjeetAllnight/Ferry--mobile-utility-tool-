# Ferry — Final MVP Implementation Report

**Date:** 2026-09-13  
**Phase:** MVP Final Sprint (Feature Completion)  
**Status:** ✅ All automated tests passing, APK builds successfully. Ready for physical device testing.

---

## Summary

This sprint completed all remaining planned MVP features. The implementation covers:

1. **Multi-File & Directory Transfers** — critical receive-side bug fixed
2. **Background Operation** — systemd service + install script
3. **Desktop Notifications** — GLib.Notification on Linux
4. **System Integration** — `.desktop` entry + install script
5. **Clipboard Sync** — bidirectional text sync, both platforms
6. **UI Polish** — toggle, labels, status text

---

## 1. Critical Bug Fix: Android Incoming Batch Transfers

**Root cause:** `onBatchRequest()` in `FerryControlClient.kt` was a stub that unconditionally rejected every `BATCH_REQUEST` with `NOT_IMPLEMENTED_YET`.

**Fix:**
- `onBatchRequest()` now sends `BATCH_ACCEPT` and records `activeBatchId`, `activeBatchTotalItems`.
- `onTransferRequest()` now checks `batchId` — items belonging to the active batch skip the "busy" guard and are auto-accepted sequentially.
- `onBatchCancel()` / `onBatchComplete()` clear the active batch state.
- Fixed `sendBatch()` payload key `total_files` → `total_items` (protocol compliance with Linux peer).

---

## 2. Clipboard Sync

### Protocol (both platforms)
New message types added to `MessageType` enum (Linux) and `ProtocolConstants.MessageTypes` (Android):
- `CLIPBOARD_SYNC` — payload: `{text: str, ts: ms}`
- `CLIPBOARD_SYNC_ACK` — optional, no-op currently

### Linux (`linux/src/ferry_linux/core/clipboard.py`)
`ClipboardSyncManager`:
- Enable/disable toggle (disabled by default)
- Loop guard: text received from a peer is not echoed back to the same peer
- Size limit: 512 KiB max
- Async `on_local_clipboard_changed()` sends `CLIPBOARD_SYNC` to peer
- `on_remote_clipboard()` writes to local GTK clipboard via registered callback
- `clear_peer_state()` called on disconnect

### Linux (`window.py`)
- Clipboard Sync toggle switch in the System Status preferences group
- GTK clipboard polling every 1.5 s via `GLib.timeout_add` + `Gtk.Clipboard.read_text_async`
- Incoming clipboard text written via `Gtk.Clipboard.set()`

### Android (`FerryControlClient.kt`)
- `clipboardSyncEnabled: Boolean` — public toggle
- `remoteClipboard: StateFlow<String?>` — exposes received text to Compose UI
- `sendClipboardSync(text)` — pushes local clipboard to peer with loop guard
- `onClipboardSync()` — handles incoming `CLIPBOARD_SYNC`, updates StateFlow

### Android (`FerryApp.kt`)
- `remoteClipboard` collected as Compose state
- `LaunchedEffect(remoteClipboard)` writes text to `android.content.ClipboardManager`
- Clipboard Sync toggle card shown when session is ESTABLISHED

---

## 3. Desktop Notifications (Linux)

### `linux/src/ferry_linux/core/notifications.py`
`NotificationManager` wraps `GLib.Notification`:
- `incoming_transfer_request(device_name, file_name, file_size)` — fires when `TRANSFER_REQUEST` arrives
- `transfer_complete(file_name, success, direction)` — fires after `TRANSFER_RESULT`
- `session_established(device_name)` — reserved for pairing completion
- `pairing_request(device_name, sas)` — reserved for pairing prompt
- `clipboard_sync_received(device_name)` — reserved for clipboard updates
- Falls back to `logger.debug` if `Gio.Application` reference unavailable

**Wiring in `service.py`:**
- `self.notifications = NotificationManager()` instantiated in `__init__`
- `notifications.incoming_transfer_request(...)` called in `_on_transfer_request()`
- `notifications.transfer_complete(...)` called in `_on_transfer_complete()`

**Wiring in `app.py`:**
- `self.service.notifications._app = self` — passes the `Adw.Application` instance so `send_notification()` works.

---

## 4. System Integration

### `linux/systemd/ferry.service`
Standard systemd user unit:
- `ExecStart=%h/.local/bin/ferry --service` (headless daemon mode)
- `Restart=on-failure` with 5 s delay
- `MemoryMax=256M`
- `WantedBy=default.target`

### `linux/desktop/dev.ferry.Ferry.desktop`
XDG `.desktop` entry:
- `Exec=ferry`, `MimeType=application/octet-stream;`
- Appears in GNOME application launcher

### `linux/install_integration.sh`
Interactive install script:
- Installs systemd unit → `~/.config/systemd/user/`
- Prompts for autostart enable
- Installs `.desktop` entry → `~/.local/share/applications/`
- `--uninstall` flag for clean removal

---

## 5. Verification Results

| Check | Result |
|---|---|
| Linux unit tests | ✅ 219/219 passing |
| Android Kotlin compile | ✅ No errors |
| Android assembleDebug | ✅ BUILD SUCCESSFUL |

---

## 6. Known Remaining Items (Physical Testing)

The following require physical device verification:
- Actual clipboard sync roundtrip (Linux↔Android)
- Incoming multi-file batch on Android (BATCH_REQUEST → auto-accept flow)
- Desktop notifications appearing in GNOME notification center
- mDNS discovery reliability on real Wi-Fi
- Linux auto-start via systemd after `install_integration.sh`
