# Phase 4B: Share & Send Integration

## Objective
Improve Ferry's existing file-sharing capabilities by natively integrating with the Android Share Sheet and the Linux Desktop's file association mechanisms (`do_open`). This ensures that files can be sent into the Ferry application directly from other contexts without requiring users to launch Ferry manually and navigate a custom file picker.

## Scope & Implementation

### 1. Android ACTION_SEND Integration
- **Implementation**: The Android `AndroidManifest.xml` previously advertised an intent filter for `ACTION_SEND` (`*/*`), but the app discarded the incoming payload. We intercepted `Intent.ACTION_SEND` in `MainActivity.onCreate` and `MainActivity.onNewIntent`. The `Intent.EXTRA_STREAM` `Uri` is parsed and exposed to the Compose `FerryApp` as `sharedUri`.
- **UI Behavior**: If Ferry is opened via the Share sheet and an established connection exists, it bypasses user prompt and immediately triggers `controlClient.sendFile(sharedUri, context)`. If no active session exists, a "Pending Share" banner is shown in the UI, and the transfer will auto-send once a trusted device connects.

### 2. Linux GTK `do_open` Integration
- **Implementation**: GTK `Adw.Application` supports the `Gio.ApplicationFlags.HANDLES_OPEN` flag, which natively intercepts files passed via command-line arguments (or DBus calls) to the single-instance GTK application. We updated `FerryApplication` to use `HANDLES_OPEN` and implemented the `do_open` callback.
- **UI Behavior**: Received `Gio.File` paths are stored in `FerryMainWindow` as `_pending_send_path`. If an established connection to a peer exists, the transfer is fired off immediately using `app.service.send_file()`. Otherwise, the path is held in memory and processed as soon as a connection comes online.

### 3. Security
- The changes reuse the exact same core transfer protocol and pathing logic, meaning existing filename traversal protection, metadata parsing, and stream-handling are completely retained. The encryption stack is unaffected. No sensitive permissions are retained.

## Verification Status

### Automated Tests
- **Linux**: 219 tests executed. (Note: 3 test failures exist related to the deferred Phase 3E Linux resume issue, but are unrelated to this Share implementation).
- **Android**: `assembleDebug` builds cleanly.

### Physical Verification
- **Status**: **PASSED**
- **Manual Verification Performed**: The user manually opened an Android app, tapped Share, selected Ferry, and successfully transmitted a file to the trusted Linux device. The Ferry send flow correctly handled the incoming intent and completed the transfer.

## Known Limitations
- Sending multiple files at once (`ACTION_SEND_MULTIPLE`) is currently unsupported and must be implemented as part of the broader Phase 4A Multi-File/Directory architecture.
- The Linux `do_open` currently picks the first `Gio.File` provided. Multi-file shares are not yet batched.
