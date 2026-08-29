# Ferry Developer Guide & Toolchain Reference

This guide provides step-by-step instructions for inspecting the local environment, building, running, testing, and debugging both the Linux service/UI and the Android application.

---

## 1. Local Environment Audit

To verify your Arch Linux and Android development toolchain:

```bash
# Check Python version and GTK4 / Libadwaita introspection
python3 --version
python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Adw', '1'); from gi.repository import Gtk, Adw; print('GTK4 and Libadwaita loaded successfully!')"

# Check Java environment
java -version

# Check connected Android devices
adb devices -l

# Check Android SDK location
echo "Android SDK: ${ANDROID_HOME:-$HOME/Android/Sdk}"
```

---

## 2. Linux Service & Desktop UI

The Linux application is written in Python 3, using GTK4, Libadwaita, and asyncio.

### 2.1. Running Unit Tests
Execute the Linux test suite using standard Python `unittest`:

```bash
python3 -m unittest discover -s linux/tests -v
```

### 2.2. Running the Desktop UI
Launch the native GNOME Libadwaita desktop application:

```bash
PYTHONPATH=linux/src python3 -m ferry_linux
```

### 2.3. Running the Headless Daemon
Launch the Ferry background service without a UI:

```bash
PYTHONPATH=linux/src python3 -m ferry_linux --service
```

### 2.4. Python Linting & Formatting
```bash
# Check syntax with Python compiler
python3 -m compileall linux/src linux/tests
```

---

## 3. Android Application

The Android app is located in `android/` and built using Gradle with Android Gradle Plugin (AGP) and Kotlin Jetpack Compose.

### 3.1. Building the Debug APK
```bash
cd android
./gradlew assembleDebug
```
*Output APK location*: `android/app/build/outputs/apk/debug/app-debug.apk`

### 3.2. Running Android Unit Tests
```bash
cd android
./gradlew testDebugUnitTest
```

### 3.3. Installing to Physical Android Device via ADB
Ensure your device is connected via USB/Wi-Fi and authorized:

```bash
# Verify device connection
adb devices

# Install APK to device
adb install -r android/app/build/outputs/apk/debug/app-debug.apk

# Launch Ferry MainActivity
adb shell am start -n dev.ferry.app/.MainActivity
```

### 3.4. Inspecting Android Logs (Logcat)
```bash
# View Ferry logs in real-time
adb logcat -s FerryApp:V AndroidRuntime:E

# Dump recent logs
adb logcat -d -s FerryApp:V AndroidRuntime:E | tail -n 50
```

---

## 4. Common Troubleshooting

| Issue | Cause | Resolution |
| :--- | :--- | :--- |
| `gi.require_version('Adw', '1')` fails | Missing Libadwaita typelib | Install `libadwaita` and `python-gobject` via `pacman -S libadwaita python-gobject`. |
| `adb: no devices/emulators found` | USB debugging disabled or disconnected | Enable USB debugging in Android Developer Options and authorize desktop RSA key. |
| `SDK location not found` | Missing `local.properties` | Ensure `android/local.properties` exists with `sdk.dir=/home/<user>/Android/Sdk`. |
| `Unsupported class file major version` | Gradle / Java mismatch | Use Gradle 9.4.1+ wrapper with OpenJDK 26 (`./gradlew`). |
