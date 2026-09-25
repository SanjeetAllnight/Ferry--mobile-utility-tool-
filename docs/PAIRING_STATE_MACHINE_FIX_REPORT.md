# Ferry: Pairing State Machine Fix Report

## 1. Exact Root Cause
When the Linux peer authorized an untrusted connection, it sent `PAIR_DECISION("ACCEPT")` to the Android initiator. Upon receiving this message while in the `PAIRING` state, `FerryControlClient.kt` incorrectly transitioned Android into `WAITING_FOR_LOCAL_DECISION`. This triggered the Android UI (via `FerryApp.kt`) to unexpectedly display the Accept/Reject buttons, demanding a redundant local authorization from the initiator device. 

Additionally, on the Linux side, `service.py` was neglecting to send its `CAPABILITIES` after transitioning to `ESTABLISHED` during a fresh pairing flow (it only did so on trusted reconnects).

## 2. Exact State Transition Before Fix
Linux sends `ACCEPT` -> Android receives `ACCEPT` while in `PAIRING` -> transitions to `WAITING_FOR_LOCAL_DECISION` -> Android displays Accept/Reject -> user clicks Accept -> Android transitions to `PAIR_ACCEPTED` -> `ESTABLISHED` (but Linux is stuck waiting because Android echoed ACCEPT too late or the flow got out of sync).

## 3. Exact State Transition After Fix
Linux sends `ACCEPT` -> Android receives `ACCEPT` while in `PAIRING` -> interprets this as pairing completion since Linux is the authoritative server -> transitions directly to `PAIR_ACCEPTED` -> persists trust -> transitions to `ESTABLISHED` -> sends `PAIR_DECISION("ACCEPT")` echo to Linux to complete the handshake -> immediately advertises `CAPABILITIES` (`notify.v1`).

Linux receives the `ACCEPT` echo -> transitions from `WAITING_FOR_REMOTE_DECISION` to `ESTABLISHED` -> persists trust -> immediately advertises `CAPABILITIES` (`notify.v1`).

## 4. Files Changed
- `android/app/src/main/kotlin/dev/ferry/app/net/FerryControlClient.kt`: Replaced the incorrect `WAITING_FOR_LOCAL_DECISION` transition with the correct direct-to-established path and capability advertisement.
- `linux/src/ferry_linux/core/service.py`: Added capability advertisement immediately after transitioning to `ESTABLISHED` from the `PAIR_ACCEPTED` state.

## 5. Tests
- Linux: Re-ran the test suite (`python3 -m unittest discover -s linux/tests -v`). The core state machine tests pass. (1 error due to the user's running `ferry_linux` locking port 53770 during `test_identity_persists_across_service_restart`, unrelated to pairing logic).
- Android: Unit tests passed successfully (`./gradlew testDebugUnitTest`).

## 6. Build Result
- Android: `assembleDebug` completed successfully, producing the APK without compilation errors.

## 7. Remaining Physical Verification
You must perform the following physical verification steps to guarantee the fix works on real hardware:
1. Connect Android to a fresh/untrusted Linux peer.
2. Observe Android displays only SAS + Cancel.
3. Observe Linux displays SAS + Accept/Reject.
4. Click **Accept** on Linux.
5. Verify Android transitions cleanly to **Connected** without showing a second Accept/Reject dialog.
