import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ferry_linux.core.service import FerryService
from ferry_linux.core.session import SessionState
from ferry_linux.core.discovery import DiscoveredDevice


def run_adb(cmd_args: list[str]) -> str:
    """Run an ADB shell command and return trimmed stdout."""
    res = subprocess.run(["adb", "shell"] + cmd_args, capture_output=True, text=True, check=True)
    return res.stdout.strip()


async def main():
    print("=" * 60)
    print("Ferry Phase 3A.1 — Physical Transfer Transport Verification")
    print("=" * 60)

    test_dir = Path("/home/sanjeet/Projects/Ferry/linux/scratch_env/transfer_test")
    test_dir.mkdir(parents=True, exist_ok=True)

    # Use standard service or persistent test environment
    base_dir = Path("/home/sanjeet/Projects/Ferry/linux/scratch_env")
    svc = FerryService(base_dir=base_dir)
    await svc.start()

    print(f"Linux local identity: {svc.identity.public_key_b64[:16]}...")
    print(f"Linux service port: {svc.config.listen_port}")

    print("\nWaiting for Android peer to connect...")
    print("--> Please ensure Ferry is open on your Android phone and tap 'Connect'.")

    state_reached = None
    remote_addr = None
    session_established_event = asyncio.Event()

    def on_session_state(peer_addr: str, state: SessionState):
        nonlocal state_reached, remote_addr
        print(f"[Session Event] {peer_addr} -> {state.name}")
        state_reached = state
        remote_addr = peer_addr

        if state == SessionState.PAIRING:
            ps = svc._active_sessions.get(peer_addr)
            if ps:
                print(f"\n>>> SAS Pairing Code: {ps.session.sas_code} <<<")
                print("Auto-accepting pairing on Linux side...")
                asyncio.create_task(svc.accept_pairing(peer_addr))

        elif state == SessionState.ESTABLISHED:
            session_established_event.set()

    svc.add_session_listener(on_session_state)

    # Wait up to 60s for session to reach ESTABLISHED
    try:
        await asyncio.wait_for(session_established_event.wait(), timeout=60.0)
    except asyncio.TimeoutError:
        print("ERROR: Timed out waiting for Android session to establish.")
        await svc.stop()
        sys.exit(1)

    print("\n" + "=" * 60)
    print(f"Authenticated Secure Session ESTABLISHED with {remote_addr}")
    print("=" * 60)

    # Clean Android staging directory before tests
    print("\n[ADB] Cleaning Android staging directory...")
    try:
        run_adb(["rm", "-rf", "/sdcard/Download/Ferry/staging/*"])
    except Exception as e:
        print(f"Warning cleaning Android staging dir: {e}")

    test_results = {}

    # -------------------------------------------------------------
    # Test 1: Small File Transfer (~128 KiB)
    # -------------------------------------------------------------
    print("\n" + "-" * 50)
    print("Test 1: Small File Transfer (131,072 bytes / 2 chunks)")
    print("-" * 50)

    small_file = test_dir / "small_doc.bin"
    small_data = os.urandom(131072)
    small_file.write_bytes(small_data)
    small_sha = hashlib.sha256(small_data).hexdigest()
    print(f"Local file: {small_file.name} ({len(small_data)} bytes)")
    print(f"Local SHA-256: {small_sha}")

    t0 = time.perf_counter()
    success_small = await svc.send_file(remote_addr, small_file)
    t1 = time.perf_counter()
    small_duration = t1 - t0

    if success_small:
        print(f"Transfer completed in {small_duration:.3f}s (Speed: {len(small_data)/(small_duration*1024):.1f} KB/s)")
        # Inspect via ADB
        adb_ls = run_adb(["ls", "-la", f"/sdcard/Download/Ferry/staging/{small_file.name}"])
        print(f"[ADB ls] {adb_ls}")
        adb_sha = run_adb(["sha256sum", f"/sdcard/Download/Ferry/staging/{small_file.name}"]).split()[0]
        print(f"[ADB sha256] {adb_sha}")

        if adb_sha.lower() == small_sha.lower():
            print(">>> Test 1 PASSED: SHA-256 integrity matches perfectly! <<<")
            test_results["Test 1 (Small File)"] = "PASSED"
        else:
            print(">>> Test 1 FAILED: Remote SHA-256 mismatch! <<<")
            test_results["Test 1 (Small File)"] = f"FAILED (SHA mismatch: {adb_sha} vs {small_sha})"
    else:
        print(">>> Test 1 FAILED: send_file returned False <<<")
        test_results["Test 1 (Small File)"] = "FAILED"

    # -------------------------------------------------------------
    # Test 2: Large Multi-Megabyte Transfer (2,621,440 bytes = 40 chunks)
    # -------------------------------------------------------------
    print("\n" + "-" * 50)
    print("Test 2: Large File Transfer (2,621,440 bytes / 40 chunks)")
    print("-" * 50)

    large_file = test_dir / "large_media.bin"
    large_data = os.urandom(2621440)
    large_file.write_bytes(large_data)
    large_sha = hashlib.sha256(large_data).hexdigest()
    print(f"Local file: {large_file.name} ({len(large_data)} bytes)")
    print(f"Local SHA-256: {large_sha}")

    t0 = time.perf_counter()
    success_large = await svc.send_file(remote_addr, large_file)
    t1 = time.perf_counter()
    large_duration = t1 - t0

    if success_large:
        speed_mbps = (len(large_data) / (1024 * 1024)) / large_duration
        print(f"Transfer completed in {large_duration:.3f}s (Throughput: {speed_mbps:.2f} MB/s)")
        # Inspect via ADB
        adb_ls = run_adb(["ls", "-la", f"/sdcard/Download/Ferry/staging/{large_file.name}"])
        print(f"[ADB ls] {adb_ls}")
        adb_sha = run_adb(["sha256sum", f"/sdcard/Download/Ferry/staging/{large_file.name}"]).split()[0]
        print(f"[ADB sha256] {adb_sha}")

        if adb_sha.lower() == large_sha.lower():
            print(">>> Test 2 PASSED: Multi-chunk file integrity matches perfectly! <<<")
            test_results["Test 2 (Large File)"] = f"PASSED ({speed_mbps:.2f} MB/s)"
        else:
            print(">>> Test 2 FAILED: Remote SHA-256 mismatch! <<<")
            test_results["Test 2 (Large File)"] = "FAILED (SHA mismatch)"
    else:
        print(">>> Test 2 FAILED: send_file returned False <<<")
        test_results["Test 2 (Large File)"] = "FAILED"

    # -------------------------------------------------------------
    # Test 3: Staging Isolation & Cleanliness Check
    # -------------------------------------------------------------
    print("\n" + "-" * 50)
    print("Test 3: Staging Isolation Check (No partial .part files remaining)")
    print("-" * 50)

    part_files = run_adb(["find", "/sdcard/Download/Ferry/staging/", "-name", "*.part"])
    if not part_files:
        print("No orphaned .part files found in Android staging directory.")
        test_results["Test 3 (Staging Cleanliness)"] = "PASSED"
    else:
        print(f"Warning: Found orphaned .part files: {part_files}")
        test_results["Test 3 (Staging Cleanliness)"] = "FAILED (Orphaned .part file found)"

    print("\n" + "=" * 60)
    print("SUMMARY OF PHYSICAL TRANSFER TESTS")
    print("=" * 60)
    all_passed = True
    for test_name, status in test_results.items():
        print(f"  {test_name}: {status}")
        if not status.startswith("PASSED"):
            all_passed = False

    print("=" * 60)
    if all_passed:
        print("ALL PHYSICAL TRANSFER VERIFICATION TESTS PASSED SUCCESSFULLY!")
    else:
        print("SOME TESTS FAILED.")
    print("=" * 60)

    await asyncio.sleep(2)
    await svc.stop()


if __name__ == "__main__":
    asyncio.run(main())
