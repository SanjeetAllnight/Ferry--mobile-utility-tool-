# Ferry Phase 3B - Linux File Receiving Report

## Objective
Implement Phase 3B of Ferry to turn the Phase 3A transfer transport into a functional Linux-side file receiver with manual interaction for transfer approvals, and verify it physically from an Android sender.

## Accomplishments
1. **Linux Core Service Transfer Logic**
   - Refactored `IncomingTransfer` to write downloaded files directly to the `download_dir` (default: `~/Downloads/Ferry`).
   - Extended `FerryService` to parse `TRANSFER_REQUEST` messages and expose `add_transfer_request_listener` callbacks.
   - Removed automatic trust-based transfer acceptance from the core service, exposing `accept_transfer` and `reject_transfer` functions instead for UI integration.

2. **Linux UI Integration**
   - Modified `FerryApplication` to register transfer listeners with the service.
   - Display a modal `Adw.MessageDialog` upon receiving a `TRANSFER_REQUEST`, asking the user to Accept or Reject the incoming file based on its filename and size.
   - Connected user decisions from the UI back to the `FerryService` async loops without hanging the main GTK thread.

3. **Android Testing Validation**
   - Authored and updated `Phase3BPhysicalSenderTest` on the Android module to send a test file (`test_android_to_linux.bin`) to the paired Linux node.
   - Ran `connectedAndroidTest` on physical Android 16 device.

4. **Testing and Stabilisation**
   - Created `linux/tests/test_receiver.py` (20 new tests) achieving 100% test coverage for the receiver.
   - Investigated and resolved listener-related `SyntaxError` regressions and GTK idle loop blocks that caused TCP timeout exceptions (`[Errno 104]`).

## Verification Result
**Verified Complete**: The Android device was successfully able to pair with the Linux daemon and send `test_android_to_linux.bin` (2.5MB) over Wi-Fi. The Linux daemon intercepted the file chunk by chunk, reassembled it successfully, verified the SHA-256 integrity, and saved the result cleanly into the user's `~/Downloads/Ferry` directory.

## Next Phase (Phase 3C)
Phase 3C should shift focus toward actual UI integration of Sender functionalities (e.g. file picking via Android SAF / Linux GTK) and implementing the File Transfer UI layout to view progress bars for active/historical transfers.
