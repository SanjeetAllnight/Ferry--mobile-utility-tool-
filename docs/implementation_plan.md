# Ferry Phase 2C Implementation Plan: Interactive Trust & Pairing UX

This phase replaces the auto-trust mechanism from Phase 2B with an interactive, user-confirmed pairing process where both sides explicitly accept or reject the connection based on a matching SAS.

## User Review Required

> [!WARNING]
> **Identity Rotation (Android)**
> Importing the existing `SharedPreferences` Ed25519 key directly into `AndroidKeyStore` is unsupported without manually building an X.509 certificate chain via a 3rd party library like BouncyCastle. Furthermore, an imported key would only be software-backed, missing the security benefits of the KeyStore.
> **Decision**: Android will generate a new hardware-backed Ed25519 identity and discard the legacy software key. This will require re-pairing any previously connected devices. Is this acceptable?

> [!IMPORTANT]
> **Pairing Requirement**
> Both endpoints must explicitly accept the pairing decision. If one endpoint rejects or times out (30 seconds), the session is aborted and no trust is persisted.

## Proposed Changes

### Protocol Layer
#### [MODIFY] [models.py](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/protocol/models.py)
- Introduce a new AEAD-encrypted control message: `PAIR_DECISION` with payload `{"decision": "ACCEPT" | "REJECT"}`.

### Linux Daemon & UI
#### [MODIFY] [session.py](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/session.py)
- Expand `SessionState` to include `WAITING_FOR_LOCAL_DECISION`, `WAITING_FOR_REMOTE_DECISION`, and `PAIR_ACCEPTED`.
#### [MODIFY] [service.py](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/service.py)
- Remove auto-trust in Phase 2B. 
- Pause connection at `SessionState.PAIRING_REQUIRED`. Emit a `pairing_requested(remote_addr, name, sas)` callback.
- Add `accept_pairing(addr)` and `reject_pairing(addr)` methods.
- Handle receiving `PAIR_DECISION` from the remote peer.
#### [MODIFY] [window.py](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/ui/window.py)
- Implement an `Adw.MessageDialog` to display the SAS and prompt the user with Accept/Reject buttons.
- Add an "Unpair" button next to currently trusted devices.
#### [MODIFY] [db.py](file:///home/sanjeet/Projects/Ferry/linux/src/ferry_linux/core/db.py)
- Add a `remove_device(public_key)` function.

### Android Application
#### [MODIFY] [FerryIdentity.kt](file:///home/sanjeet/Projects/Ferry/android/app/src/main/kotlin/dev/ferry/app/security/FerryIdentity.kt)
- Migrate key generation to `KeyPairGenerator.getInstance("Ed25519", "AndroidKeyStore")`.
- Remove `SharedPreferences` keys.
- Check and log `KeyInfo.isInsideSecureHardware`.
#### [MODIFY] [FerryControlClient.kt](file:///home/sanjeet/Projects/Ferry/android/app/src/main/kotlin/dev/ferry/app/net/FerryControlClient.kt)
- Remove auto-trust. Expose a `PairingRequest` state with the SAS.
- Wait for user action before sending `PAIR_DECISION`.
#### [MODIFY] [FerryApp.kt](file:///home/sanjeet/Projects/Ferry/android/app/src/main/kotlin/dev/ferry/app/ui/FerryApp.kt) (or related UI files)
- Implement a Compose `AlertDialog` displaying the SAS with Accept/Reject buttons.
- Expose an "Unpair" action for known devices.

## Verification Plan

### Automated Tests
- `python3 -m unittest discover -s linux/tests -v`
- Modify `test_control_plane.py` to mock the user clicking "Accept".
- `cd android && ./gradlew testDebugUnitTest`

### Manual Verification
1. Launch Linux daemon and UI (`python3 -m ferry_linux`).
2. Build and launch Android app on physical device.
3. Click "Connect" on Android.
4. Verify both Linux and Android present a dialog with matching SAS codes.
5. Click "Accept" on both. Verify session transitions to `ESTABLISHED`.
6. Disconnect. Reconnect and verify it bypasses the SAS check.
7. Click "Unpair" and verify the next connection prompts for SAS again.
