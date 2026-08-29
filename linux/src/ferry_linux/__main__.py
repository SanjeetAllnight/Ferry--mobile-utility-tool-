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
    parser = argparse.ArgumentParser(description="Ferry - Local Android ↔ Arch Linux Integration")
    parser.add_argument("--service", action="store_true", help="Run Ferry headless service daemon")
    parser.add_argument("--test-mode", action="store_true", help="Initialize and exit immediately for validation")
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
    return app.run([sys.argv[0]] + unknown)


if __name__ == "__main__":
    sys.exit(main())
