# Ferry Phase 3B.2 — Android App Packaging & UI Readiness Audit Report

## 1. Android Launcher Diagnosis
- **Investigation**: Prior to this audit, `dev.ferry.app` was not present in the Android application launcher on the connected device because the application package had not been installed via `adb install -r app-debug.apk` following the execution of automated instrumentation tests (instrumentation runners run via isolated test packages).
- **Manifest & Resource Analysis**:
  - `AndroidManifest.xml` correctly declared `dev.ferry.app.MainActivity` with `android:exported="true"`, `action.MAIN`, and `category.LAUNCHER`.
  - The application label `@string/app_name` resolves properly to `"Ferry"`.
  - Added `@mipmap/ic_launcher_round` (`ic_launcher_round.xml`) and mapped `android:roundIcon="@mipmap/ic_launcher_round"` in `AndroidManifest.xml` to guarantee complete compatibility across launchers expecting adaptive or round icons.

## 2. Actual Installed Package & Activity
- **Package Name**: `dev.ferry.app`
- **Application ID**: `dev.ferry.app`
- **Main Launcher Activity**: `dev.ferry.app.MainActivity` (component: `dev.ferry.app/.MainActivity`)
- **Application Label**: `Ferry`

## 3. Fixes Made
1. **Android Adaptive Round Icon**: Created `android/app/src/main/res/mipmap-anydpi-v26/ic_launcher_round.xml` and wired `android:roundIcon="@mipmap/ic_launcher_round"` in `AndroidManifest.xml`.
2. **Linux UI Dialog Cleanup**: Removed temporary test-bypass logic (`AUTO-ACCEPTING PAIRING` / `AUTO-ACCEPTING TRANSFER`) from `linux/src/ferry_linux/ui/window.py` so that interactive Libadwaita dialogs (`Adw.MessageDialog`) are presented to the desktop user for pairing and transfer confirmations.
3. **Receiving Destination Path Normalization**: Reconciled `linux/src/ferry_linux/core/config.py` default `download_dir` to `~/Downloads/Ferry` (`Path.home() / "Downloads" / "Ferry"`), aligning code, configuration, tests, and documentation.
4. **Documentation Sync**: Updated `docs/PROJECT_STATE.md`, `docs/TESTING.md`, and `docs/PHASE_3B_REPORT.md` with accurate test counts (147 Linux tests, 50 Android tests) and standardized destination paths.

## 4. Physical Android Verification
- Built debug APK: `cd android && ./gradlew assembleDebug`
- Installed via ADB: `adb install -r android/app/build/outputs/apk/debug/app-debug.apk` (Result: `Success`)
- Verified launcher registration with package manager (`adb shell dumpsys package dev.ferry.app` confirms `dev.ferry.app/.MainActivity` in `Activity Resolver Table` for `android.intent.action.MAIN` + `android.intent.category.LAUNCHER`).
- Launched application: `adb shell am start -n dev.ferry.app/.MainActivity` (Result: `Starting: Intent { cmp=dev.ferry.app/.MainActivity }`, confirmed focused window `Window{... dev.ferry.app/dev.ferry.app.MainActivity}`).

## 5. Linux UI Status
- **Window**: GTK4 / Libadwaita desktop application (`FerryMainWindow`).
- **Available Actions**:
  - View Discovery Status & Wire Protocol version.
  - View and Unpair Trusted Devices (stored in SQLite).
  - View Nearby Discovered Peers (untrusted / mDNS advertised).
  - Display SAS verification pairing confirmation dialog (`Adw.MessageDialog`) on incoming connection requests.
  - Display file transfer confirmation dialog (`Adw.MessageDialog`) showing incoming device name, file name, and human-readable size with "Accept" and "Reject" actions.

## 6. Android UI Status
- **Screen**: Jetpack Compose single-activity interface (`FerryApp.kt`).
- **Available Actions**:
  - View dynamic local discovery state, device ID, and service type.
  - View nearby discovered Arch Linux / Ferry peers with live connection badges (`Available`, `Pairing…`, `● Secure`).
  - Initiate connection (`Connect`) to discovered desktop peer.
  - Terminate connection (`Disconnect`).
  - View interactive 6-digit SAS pairing dialog with "Accept" and "Reject" buttons.

## 7. Actual Receiving Destination
- **Normalized Destination**: `~/Downloads/Ferry/` (`Path.home() / "Downloads" / "Ferry"`).
- **Safety Enforcement**: In-band sanitization strips path traversal sequences (e.g. `../../etc/passwd` → `passwd`), control characters, and handles collisions safely.

## 8. Documentation Corrections
- `docs/PROJECT_STATE.md`: Synchronized Linux test suite count to 147 tests.
- `docs/TESTING.md`: Added `test_receiver.py` documentation and updated verification command to 147 tests.
- `docs/PHASE_3B_REPORT.md`: Confirmed destination path consistency (`~/Downloads/Ferry`).

## 9. Test Results
- **Linux Unit & Integration Tests**: 147 / 147 passed (`python3 -m unittest discover -s linux/tests -v`, 0 failures, 0 errors).
- **Android Unit Tests**: 50 / 50 passed (`./gradlew testDebugUnitTest`, `BUILD SUCCESSFUL`).
- **Android Debug Build**: `BUILD SUCCESSFUL` (`./gradlew assembleDebug`).

## 10. Remaining UI Limitations (Deferred to Phase 3C)
- No user-facing file picker (Android SAF `ACTION_OPEN_DOCUMENT` / `ACTION_SEND` or GTK4 `Gtk.FileDialog`) is yet attached to the main UI.
- No live progress bar or active transfer dashboard is rendered in either UI during transfer streaming.
- Transfer resumption and pause/resume mechanisms are pending.
- These features are strictly scheduled for Phase 3C.
