"""
Ferry Linux Main Entry Point.
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
    parser.add_argument("--test-mode", action="store_true", help="Initialize and exit immediately for validation")
    parser.add_argument("--send", metavar="FILE", nargs="+", help="Open Ferry and queue file(s) for sending (Nautilus/CLI integration)")
    args, unknown = parser.parse_known_args()

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

    # Default: Run UI
    from .ui.app import FerryApplication
    app = FerryApplication()
    if args.test_mode:
        print("Ferry UI test mode: initialized successfully.")
        return 0

    # If --send was given, turn the file paths into GFile arguments for do_open
    argv = [sys.argv[0]] + unknown
    if args.send:
        from gi.repository import Gio
        gfiles = []
        for p in args.send:
            from pathlib import Path as _Path
            gf = Gio.File.new_for_path(str(_Path(p).resolve()))
            gfiles.append(gf)
        # do_open is called by GTK when the app receives files; simulate it after activate
        def _queue_send():
            if app.window and gfiles:
                from gi.repository import GLib
                def _send_deferred():
                    app.do_open(gfiles, len(gfiles), "")
                    return False
                GLib.idle_add(_send_deferred)
        app._pending_send_callback = _queue_send
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
