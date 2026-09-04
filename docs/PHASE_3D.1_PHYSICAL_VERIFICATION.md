# Phase 3D.1 Physical Reliability Acceptance Verification

**Date:** 2026-09-05  
**Tester:** Automated Verification  
**Devices:** 
- Host: Arch Linux x86_64 
- Remote: Realme RMX3870 (Android 16)
- Network: Local Wi-Fi (10.213.207.x subnet)

---

## Objective

Physically verify the three Phase 3D acceptance criteria for transfer reliability: mid-transfer disconnect (PT-1), user-facing cancellation (PT-2), and recovery/successful retry after a failure (PT-3).

---

## 1. Preparation

- Deployed Ferry debug APK (`dev.ferry.app`) to the physical Android device.
- Generated a 55 MiB random data file (`test_55mb_ferry.bin`, SHA-256: `35145aa9443f088e34a1b071a8bc16145cd9cff29596e307b2e62e29d2df0a3d`).
- Launched Ferry desktop client on Arch Linux in UI mode.
- Verified mDNS service discovery (`_ferry._tcp.local.`) and trusted connection establishment between devices.

---

## 2. Test Execution & Results

### PT-1: Disconnect During Transfer
- **Action**: Initiated 55 MiB transfer from Android to Linux. Once chunks were actively streaming, the Android device's Wi-Fi was forcibly disconnected.
- **Result**: ✅ PASS
- **Observations**: 
  - Linux `Session loop error ... Connection reset by peer`.
  - Transfer state transitioned to `FAILED`.
  - The partial download file (`~/Downloads/Ferry/staging/32eef3f5-*.part`) was successfully removed.
  - The SQLite `transfer_history` table recorded the transfer as `FAILED`.
  - Linux UI remained stable and usable.

### PT-2: User-Facing Cancellation
- **Action**: Android Wi-Fi reconnected. Initiated another 55 MiB transfer. While actively streaming, the user tapped the "Cancel" button on the UI.
- **Result**: ✅ PASS
- **Observations**:
  - Linux received `TRANSFER_CANCEL` message with `reason=USER_CANCELLED`.
  - Transfer state transitioned to `CANCELLED`.
  - The partial download file (`~/Downloads/Ferry/staging/12435ba7-*.part`) was immediately removed.
  - The SQLite `transfer_history` table recorded the transfer as `CANCELLED`.
  - Both UIs returned to an idle, usable state.

### PT-3: Recovery After Failure
- **Action**: After the previous failures, initiated a new 55 MiB transfer of the exact same file. Allowed the transfer to run to completion without interruption.
- **Result**: ✅ PASS
- **Observations**:
  - Transfer received a brand-new ID (`239e9bd8-c58d-4840-9e06-103c4629bbf4`).
  - Transfer completed successfully and atomic rename succeeded.
  - Final file size verified as exactly 57,671,680 bytes.
  - SHA-256 hash verified as `35145aa9443f088e34a1b071a8bc16145cd9cff29596e307b2e62e29d2df0a3d` (perfect match).
  - The SQLite `transfer_history` table correctly recorded the transfer as `COMPLETED`.
  - Prior `FAILED` and `CANCELLED` entries remained intact and unmodified in the history.

---

## 3. Final Artifacts

### Filesystem Verification
```
$ ls -la ~/Downloads/Ferry
-rw-r--r-- 1 sanjeet sanjeet 57671680 Sep  5 00:03 test_55mb_ferry.bin
drwxr-xr-x 1 sanjeet sanjeet       50 Sep  4 23:56 staging/  (Empty)
```

### Database Verification
```
transfer_id                           file_name            status
------------------------------------  -------------------  ---------
239e9bd8-c58d-4840-9e06-103c4629bbf4  test_55mb_ferry.bin  COMPLETED
12435ba7-b61b-424c-ae68-b663d35374f2  test_55mb_ferry.bin  CANCELLED
32eef3f5-0645-42b9-9a32-b613705833b6  test_55mb_ferry.bin  FAILED
```

---

## 4. Conclusion

The Phase 3D reliability hardening mechanisms (error transitions, cleanup routines, timeout guards, and cancellation pipelines) work as expected under real-world physical failure conditions.

**PHASE 3D FULLY VERIFIED**
