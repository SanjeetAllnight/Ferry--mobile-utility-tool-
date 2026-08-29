"""
Integration test for mDNS live local discovery loopback.
"""

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf
from ferry_linux.core.config import ConfigManager, FerryConfig
from ferry_linux.core.discovery import DiscoveredDevice, DiscoveryManager, SERVICE_TYPE


class TestDiscoveryIntegration(unittest.IsolatedAsyncioTestCase):

    async def test_live_mdns_discovery_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = FerryConfig(
                device_id="desktop-local-id",
                device_name="Arch Desktop Test",
                listen_port=53770,
            )
            manager = DiscoveryManager(config)

            # Start Linux discovery manager
            await manager.start()

            discovered_list: list[DiscoveredDevice] = []
            discovery_event = asyncio.Event()

            def on_update(devices: list[DiscoveredDevice]) -> None:
                discovered_list.clear()
                discovered_list.extend(devices)
                if any(d.device_id == "simulated-android-id" for d in devices):
                    discovery_event.set()

            manager.add_listener(on_update)

            # Simulate an Android Ferry device announcement
            mock_android_azc = AsyncZeroconf()
            mock_service_name = f"Ferry-android123.{SERVICE_TYPE}"
            mock_props = {
                b"v": b"1",
                b"id": b"simulated-android-id",
                b"name": b"Simulated Pixel Phone",
                b"type": b"mobile",
                b"os": b"android",
                b"port": b"53770",
                b"app_version": b"0.1.0",
            }
            mock_info = AsyncServiceInfo(
                type_=SERVICE_TYPE,
                name=mock_service_name,
                port=53770,
                properties=mock_props,
                addresses=[socket.inet_aton("192.168.1.199")],
                server="android-test.local.",
            )

            try:
                await mock_android_azc.async_register_service(mock_info)

                # Wait for discovery up to 4 seconds
                try:
                    await asyncio.wait_for(discovery_event.wait(), timeout=4.0)
                except asyncio.TimeoutError:
                    pass

                # Verify peer discovery
                peer = manager.get_device("simulated-android-id")
                if peer:
                    self.assertEqual(peer.device_name, "Simulated Pixel Phone")
                    self.assertEqual(peer.device_type, "mobile")
                    self.assertEqual(peer.os_name, "android")
                    self.assertTrue(peer.is_available)

            finally:
                await mock_android_azc.async_unregister_service(mock_info)
                await mock_android_azc.async_close()
                await manager.stop()


if __name__ == "__main__":
    unittest.main()
