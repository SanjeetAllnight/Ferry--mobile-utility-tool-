"""
Ferry Linux Core Service Daemon.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Dict, Optional

from .config import ConfigManager, FerryConfig
from .db import DatabaseManager, TrustedDevice
from .discovery import DiscoveredDevice, DiscoveryManager

logger = logging.getLogger("ferry.service")


class FerryService:
    """Core daemon managing local networking, pairing, and transfers."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.config_manager = ConfigManager(base_dir=base_dir)
        self.config: FerryConfig = self.config_manager.load_config()
        self.db = DatabaseManager(self.config_manager.db_file)
        self.discovery = DiscoveryManager(self.config)
        self._is_running = False
        self._server: Optional[asyncio.AbstractServer] = None
        self._active_tasks: set[asyncio.Task] = set()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def discovered_devices(self) -> list[DiscoveredDevice]:
        """Return list of currently discovered peers on local network."""
        return self.discovery.get_devices()

    async def start(self) -> None:
        """Start the Ferry service background loops and local discovery."""
        if self._is_running:
            return

        logger.info("Starting Ferry Service on port %d...", self.config.listen_port)
        self._is_running = True

        # Ensure download directory exists
        Path(self.config.download_dir).mkdir(parents=True, exist_ok=True)

        # Start mDNS discovery manager
        await self.discovery.start()

        logger.info("Ferry Service is active. Device ID: %s, Name: %s",
                    self.config.device_id or "unassigned", self.config.device_name)

    async def stop(self) -> None:
        """Gracefully stop the service, discovery, and cancel background tasks."""
        if not self._is_running:
            return

        logger.info("Stopping Ferry Service...")
        self._is_running = False

        # Stop discovery manager
        await self.discovery.stop()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        for task in list(self._active_tasks):
            task.cancel()

        logger.info("Ferry Service stopped successfully.")

    async def run_forever(self) -> None:
        """Run the service until SIGINT/SIGTERM is received."""
        await self.start()
        stop_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        await stop_event.wait()
        await self.stop()
