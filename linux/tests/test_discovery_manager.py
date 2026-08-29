"""
Unit tests for Ferry DiscoveryManager and service state changes.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo
from ferry_linux.core.config import FerryConfig
from ferry_linux.core.discovery import (
    DiscoveredDevice,
    DiscoveryManager,
    PROTOCOL_VERSION,
    SERVICE_TYPE,
)


class TestDiscoveryManager(unittest.IsolatedAsyncioTestCase):

    def setUp(self) -> None:
        self.config = FerryConfig(
            device_id="my-linux-desktop-uuid",
            device_name="My Arch Desktop",
            listen_port=53770,
        )
        self.manager = DiscoveryManager(self.config)

    def test_initial_state(self) -> None:
        self.assertFalse(self.manager.is_running)
        self.assertEqual(len(self.manager.get_devices()), 0)

    def test_listener_registration(self) -> None:
        received = []
        callback = lambda devs: received.append(devs)

        self.manager.add_listener(callback)
        self.assertEqual(len(received), 1)  # Called immediately with empty list

        self.manager.remove_listener(callback)

    async def test_handle_peer_added_and_removed(self) -> None:
        # Mock AsyncZeroconf
        mock_azc = MagicMock()
        self.manager.azc = mock_azc
        self.manager._is_running = True

        service_name = "Ferry-remote._ferry._tcp.local."

        with patch("ferry_linux.core.discovery.AsyncServiceInfo") as mock_info_cls:
            mock_info = MagicMock()
            mock_info.async_request = AsyncMock(return_value=True)
            mock_info.name = service_name
            mock_info.type = SERVICE_TYPE
            mock_info.port = 53770
            mock_info.properties = {
                b"v": b"1",
                b"id": b"remote-phone-uuid",
                b"name": b"Remote Phone",
                b"type": b"mobile",
                b"os": b"android",
                b"port": b"53770",
            }
            mock_info.parsed_scoped_addresses.side_effect = lambda ip_version: (
                ["192.168.1.120"] if ip_version == IPVersion.V4Only else []
            )
            mock_info_cls.return_value = mock_info

            # Handle Added state
            await self.manager._async_handle_state_change(
                SERVICE_TYPE, service_name, ServiceStateChange.Added
            )

            devices = self.manager.get_devices()
            self.assertEqual(len(devices), 1)
            self.assertEqual(devices[0].device_id, "remote-phone-uuid")
            self.assertEqual(devices[0].device_name, "Remote Phone")
            self.assertEqual(devices[0].device_type, "mobile")

            # Handle Removed state
            await self.manager._async_handle_state_change(
                SERVICE_TYPE, service_name, ServiceStateChange.Removed
            )

            devices_after = self.manager.get_devices()
            self.assertEqual(len(devices_after), 0)

    async def test_self_advertisement_ignored(self) -> None:
        mock_azc = MagicMock()
        self.manager.azc = mock_azc
        self.manager._is_running = True

        own_service_name = "Ferry-myself._ferry._tcp.local."

        with patch("ferry_linux.core.discovery.AsyncServiceInfo") as mock_info_cls:
            mock_info = MagicMock()
            mock_info.async_request = AsyncMock(return_value=True)
            mock_info.name = own_service_name
            mock_info.type = SERVICE_TYPE
            mock_info.port = 53770
            mock_info.properties = {
                b"v": b"1",
                b"id": self.config.device_id.encode("utf-8"),  # Same as self
                b"name": self.config.device_name.encode("utf-8"),
                b"type": b"desktop",
                b"os": b"archlinux",
            }
            mock_info.parsed_scoped_addresses.side_effect = lambda ip_version: (
                ["192.168.1.100"] if ip_version == IPVersion.V4Only else []
            )
            mock_info_cls.return_value = mock_info

            await self.manager._async_handle_state_change(
                SERVICE_TYPE, own_service_name, ServiceStateChange.Added
            )

            # Must not be added
            self.assertEqual(len(self.manager.get_devices()), 0)


if __name__ == "__main__":
    unittest.main()
