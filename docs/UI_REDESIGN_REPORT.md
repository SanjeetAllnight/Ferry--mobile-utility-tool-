# UI Redesign Report

**Date**: [Current Date]

## Overview
The Ferry applications on Android (Jetpack Compose) and Linux (GTK4 / Libadwaita) have been completely visually redesigned to implement the "Stitch" UI language defined in `ferryUI/`.

The overarching aesthetic is "Sovereign & Private": strict, minimal, monochrome interfaces that emphasize technical utility over playful consumer aesthetics.

## Changes Implemented

### Android (Jetpack Compose)
- **Theme & Colors**: Overrode Material 3 defaults with a strict monochromatic dark palette (`#0A0A0A` to `#131313` backgrounds, pure white text). Disabled Dynamic Color (Monet).
- **Typography**: Removed standard Android sans-serif rounded fonts in favor of geometric sans-serif (Inter style) and strict monospace for technical labels (IPs, sizes).
- **Layout Restructuring**:
  - Removed standard floating action buttons (FABs).
  - Main transfer interface now uses a massive, flat "SEND FILES" button directly on the active peer card.
  - Replaced standard rounded cards with `0.dp` / `4.dp` corners and precise `1.dp` borders.
  - Replaced the Pairing Dialog with a stark, flat UI emphasizing the pairing code in massive typography.

### Linux (GTK4 / Libadwaita)
- **Custom CSS Engine**: Injected `style.css` via `Gtk.CssProvider` to strip Libadwaita of its standard GNOME HIG rounded corners and gradients.
- **Visual Overrides**:
  - Background set to `#0A0A0A`.
  - Window borders and card backgrounds converted to sharp, flat monochrome colors.
- **Component Replacement**:
  - Automatically patched `window.py` to replace standard GNOME layout containers (`Adw.PreferencesGroup`, `Adw.ActionRow`, `Adw.ExpanderRow`) with custom `FlatCardBox` and `FlatActionRow` containers based on `Gtk.Box`.
  - Buttons styled as `.primary` (white bg, black text) and `.secondary` (transparent bg, white border).
  - Maintained all IPC bindings and dynamic UI updates (transfers, device discovery) without breaking logic.

## Verification
- **Automated Tests**:
  - Android Unit Tests (`./gradlew testDebugUnitTest`): Passed
  - Linux Unit Tests (`python3 -m unittest discover -s linux/tests -v`): Passed
- **Builds**:
  - Android Debug APK compiled successfully.
  - No syntax or structural errors found in the Python codebase.

## Known Limitations / Notes
- The GTK4 application now heavily overrides standard GNOME Human Interface Guidelines. While it successfully achieves the cross-platform "Stitch" aesthetic, it may feel alien to users accustomed to standard native Linux applications.
- Physical device testing is deferred to the user as per core operating constraints.
