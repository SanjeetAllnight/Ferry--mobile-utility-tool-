# Final Branding Report

## Visible Branding Changes
- **Android App Name**: Replaced `Ferry` with `Raven` in `android/app/src/main/res/values/strings.xml` (`app_name`).
- **Linux Window Titles & Labels**: Replaced user-facing strings in `linux/src/ferry_linux/ui/window.py` (e.g. main window title, status banner, empty state messages, settings dialog).
- **Linux Desktop Entry**: Updated `Name=Raven` in `linux/desktop/dev.ferry.Ferry.desktop`.
- **Documentation**: Updated `README.md` and `PROJECT_STATE.md` to reflect the new application name `Raven`, while preserving historical phase descriptions where appropriate.

## Logo / Icon Changes
- **Android Adaptive Icons**: Replaced `ic_launcher_foreground.xml` with scaled raster PNG versions of the Raven logo across all `mipmap-*dpi` directories, ensuring proper centering and sizing for adaptive masks. Retained the existing background color `#0B57D0`.
- **Linux Desktop Icon**: Placed the resized (256x256) Raven logo in `linux/desktop/dev.ferry.Ferry.png` and updated `linux/install_integration.sh` to install this icon to `~/.local/share/icons/hicolor/256x256/apps/`. Updated the `.desktop` entry to reference `Icon=dev.ferry.Ferry`.

## Internal Identifiers Intentionally Preserved
- `dev.ferry.app` (Android package)
- `dev.ferry.Ferry` (Linux IPC / D-Bus app id)
- `_ferry._tcp.local.` (mDNS discovery identifier)
- `dev.ferry.v1` (Protocol version string)
- Internal class and file names (`FerryApplication`, `FerryService`, `FerryControlClient`, etc.)
- Database tables, columns, and preferences keys.

## Build and Test Status
- **Android**: `testDebugUnitTest` and `assembleDebug` executed successfully. The APK builds without errors and correctly packages the new icon and app name.
- **Linux**: The Python unit test suite executed and passed successfully.
- Both platforms maintain perfect functional parity and backward compatibility as internal protocols were not altered.

## Files Changed
- `android/app/src/main/res/values/strings.xml`
- `android/app/src/main/res/mipmap-*/ic_launcher_foreground.png` (Created)
- `android/app/src/main/res/drawable/ic_launcher_foreground.xml` (Deleted)
- `linux/src/ferry_linux/ui/window.py`
- `linux/src/ferry_linux/ui/FerryApp.kt`
- `linux/desktop/dev.ferry.Ferry.desktop`
- `linux/desktop/dev.ferry.Ferry.png` (Created)
- `linux/install_integration.sh`
- `README.md`
- `docs/PROJECT_STATE.md`
