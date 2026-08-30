# Phase 2C.3 — Physical Pairing Deadlock Bug Report & Verification

**Date:** 2026-08-30  
**Phase:** 2C.3 (Interactive Trust & Pairing Integration)  
**Status:** RESOLVED & VERIFIED

---

## 1. Observed Failure

During real-device interactive pairing between physical Android (`Realme RMX3870`, Android 16) and Linux desktop (`archnoir`):
1. Android discovered Linux via mDNS and the user tapped **Connect**.
2. Both endpoints completed the cryptographic handshake (AKE) and derived the identical 6-digit Short Authentication String (SAS).
3. Android presented the SAS dialog, and the user tapped **Accept**.
4. Android transitioned to `WAITING_FOR_REMOTE_DECISION` and sent `PAIR_DECISION: {"decision": "ACCEPT"}` to Linux.
5. Linux received the acceptance frame: `Received PAIR_DECISION: ACCEPT from archnoir (Ferry)`.
6. However, Linux never presented the pairing confirmation dialog on desktop, and the session entered a permanent deadlock where Android remained waiting in `PAIRING / WAITING_FOR_REMOTE_DECISION` and neither side reached `ESTABLISHED` nor persisted trust.

---

## 2. Root Cause Analysis

Inspection of the Linux desktop application logs revealed an uncaught `TypeError` in `ferry_linux/ui/window.py` on the GTK main loop thread:

```text
Traceback (most recent call last):
  File "/home/sanjeet/Projects/Ferry/linux/src/ferry_linux/ui/window.py", line 217, in handle_session_state
    dialog.present(self)
    ~~~~~~~~~~~~~~^^^^^^
TypeError: Gtk.Window.present() takes exactly 1 argument (2 given)
```

### Mechanism of Failure:
1. When `SessionState.PAIRING` was dispatched to `FerryMainWindow.handle_session_state`, it constructed `Adw.MessageDialog` and attempted to display it with `dialog.present(self)`.
2. In GTK4 / Libadwaita (PyGObject), `Gtk.Window.present()` / `Adw.MessageDialog.present()` takes no arguments (or 1 argument in Python including `self`). Passing `self` (the parent window) supplied 2 positional arguments, raising `TypeError`.
3. Because the exception aborted `handle_session_state`, the desktop dialog was never rendered on screen.
4. When Android sent `PAIR_DECISION: ACCEPT`, Linux transitioned from `PAIRING` to `WAITING_FOR_LOCAL_DECISION`.
5. Because the dialog had crashed, the Linux user was given no UI controls to accept or reject the pairing request.
6. The session deadlocked: Android was waiting for Linux's `PAIR_DECISION`, while Linux core was waiting for local user acceptance from a dialog that was never presented.

---

## 3. Affected Components

- **`linux/src/ferry_linux/ui/window.py`**:
  - `Adw.MessageDialog` instantiation and presentation.
  - Lifecycle management of active pairing dialogs across session state transitions.

---

## 4. Exact Fix

1. **Proper Libadwaita Dialog Configuration & Presentation**:
   - Configured `Adw.MessageDialog(transient_for=self, ...)` with parent window association.
   - Fixed `dialog.present()` to be called without parameters.

2. **Session Lifecycle Dialog Tracking**:
   - Added `self._pairing_dialogs: dict[str, Adw.MessageDialog]` to `FerryMainWindow`.
   - Prevented duplicate dialogs if session notifications re-trigger in `PAIRING` or `WAITING_FOR_LOCAL_DECISION`.
   - Automatically close/clean up open pairing dialogs when session reaches terminal states (`ESTABLISHED`, `FAILED`, `DISCONNECTED`, `CLOSING`).

```python
        if state in (SessionState.PAIRING, SessionState.WAITING_FOR_LOCAL_DECISION):
            if remote_addr in self._pairing_dialogs:
                return

            ps = app.service._active_sessions.get(remote_addr)
            if not ps:
                return

            sas = ps.session.sas_code
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading=f"Pair with {ps.remote_device_name}?",
                body=f"Verify that the following 6-digit code matches the one shown on {ps.remote_device_name}:\n\n<span size='xx-large' weight='bold'>{sas}</span>",
                body_use_markup=True
            )
            dialog.add_response("reject", "Reject")
            dialog.add_response("accept", "Accept")
            dialog.set_response_appearance("reject", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("accept", Adw.ResponseAppearance.SUGGESTED)

            def on_response(dlg, response_id):
                self._pairing_dialogs.pop(remote_addr, None)
                loop = app.get_loop()
                if response_id == "accept":
                    asyncio.run_coroutine_threadsafe(app.service.accept_pairing(remote_addr), loop)
                else:
                    asyncio.run_coroutine_threadsafe(app.service.reject_pairing(remote_addr), loop)

            dialog.connect("response", on_response)
            self._pairing_dialogs[remote_addr] = dialog
            dialog.present()
```

---

## 5. Resulting State Machine & Symmetry Verification

The pairing lifecycle is strictly symmetric:
- **Both endpoints must explicitly accept** the SAS before trust is persisted:
  $$\text{Local Accept} \land \text{Remote Accept} \implies \text{Trust Persisted} \land \text{ESTABLISHED}$$
- If local accepts first: `PAIRING` $\to$ `WAITING_FOR_REMOTE_DECISION` $\to$ receives remote `ACCEPT` $\to$ `PAIR_ACCEPTED` $\to$ `ESTABLISHED`.
- If remote accepts first: `PAIRING` $\to$ `WAITING_FOR_LOCAL_DECISION` $\to$ user accepts locally $\to$ `PAIR_ACCEPTED` $\to$ `ESTABLISHED`.
- If either side rejects: `PAIR_DECISION: REJECT` is transmitted $\to$ session transitions to `FAILED` / `DISCONNECTED` $\to$ **no trust is persisted**.

---

## 6. Automated Regression Tests

Added test cases to `linux/tests/test_control_plane.py`:
1. `test_pairing_both_accept_remote_first`: Verifies state machine and trust persistence when the remote peer accepts before the local endpoint.
2. `test_pairing_local_accept_remote_reject`: Verifies that local acceptance combined with remote rejection results in no trust persistence in SQLite.
3. `test_pairing_remote_accept_local_reject`: Verifies that remote acceptance followed by local rejection persists no trust.
4. `test_unpair_device_requires_new_pairing`: Verifies that removing a device from the database forces subsequent connections to trigger SAS verification again.

### Test Results:
- **Linux Test Suite**: 127 tests passed in 11.7s (0 failures, 0 errors).
- **Android Unit Tests**: 50 tests passed (`./gradlew testDebugUnitTest`).
- **Android APK Build**: Successful debug build (`./gradlew assembleDebug`).
