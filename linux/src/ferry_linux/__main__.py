"""
Ferry Linux Main Entry Point.

Modes:
  ferry            — Launch UI (IPC client; auto-starts daemon if not running)
  ferry --service  — Run headless daemon (binds TCP + IPC socket)
  ferry --send <files>  — Pass files to the running UI / queue for send
  FERRY_IN_PROCESS=1 ferry  — Legacy in-process mode (dev/testing)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ferry - Local Android ↔ Linux File Transfer")
    parser.add_argument("--service", action="store_true", help="Run Ferry headless service daemon")
    parser.add_argument("--in-process", action="store_true",
                        help="Run UI with in-process service (legacy, same as FERRY_IN_PROCESS=1)")
    parser.add_argument("--test-mode", action="store_true",
                        help="Initialize and exit immediately for validation")
    parser.add_argument("--send", metavar="FILE", nargs="+",
                        help="Open Ferry and queue file(s) for sending (Nautilus/CLI integration)")
    args, unknown = parser.parse_known_args()

    # ── Service / daemon mode ──────────────────────────────────────────────
    if args.service:
        from .core.service import FerryService
        service = FerryService()
        if args.test_mode:
            print("Ferry service test mode: initialized successfully.")
            return 0
        try:
            asyncio.run(service.run_forever())
            return 0
        except KeyboardInterrupt:
            return 0

    # ── UI mode ───────────────────────────────────────────────────────────
    import os
    if args.in_process:
        os.environ["FERRY_IN_PROCESS"] = "1"

    from .ui.app import FerryApplication
    app = FerryApplication()
    if args.test_mode:
        print("Ferry UI test mode: initialized successfully.")
        return 0

    # Build argv for GTK (strip our custom flags, keep unknown args)
    argv = [sys.argv[0]] + unknown

    # Handle --send: queue file(s) for sending once the UI is open
    if args.send:
        from pathlib import Path as _Path
        paths = [str(_Path(p).resolve()) for p in args.send]

        def _queue_send():
            if app.window:
                if len(paths) == 1:
                    from gi.repository import GLib
                    GLib.idle_add(app.window.handle_pending_send, paths[0])
                else:
                    from gi.repository import GLib
                    GLib.idle_add(app.window.handle_pending_send_batch, paths)

        app._pending_send_callback = _queue_send

    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
