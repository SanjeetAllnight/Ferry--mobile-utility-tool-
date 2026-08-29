"""
Unit tests for Ferry DiscoveredDevice model and TXT property decoding.
"""

import socket
import unittest
from unittest.mock import MagicMock

from zeroconf.asyncio import AsyncServiceInfo
from ferry_linux.core.discovery import (
    DiscoveredDevice,
    PROTOCOL_VERSION,
    SERVICE_TYPE,
)


class TestDiscoveryModels(unittest.TestCase):

    def _create_mock_service_info(
        self,
        name: str = "Ferry-test._ferry._tcp.local.",
        properties: dict | None = None,
        addresses: list[bytes] | None = None,
        port: int = 53770,
    ) -> AsyncServiceInfo:
        if properties is None:
            properties = {
                b"v": b"1",
                b"id": b"phone-uuid-12345",
                b"name": b"Pixel 9 Pro",
                b"type": b"mobile",
                b"os": b"android",
                b"port": b"53770",
            }
        if addresses is None:
            addresses = [socket.inet_aton("192.168.1.150")]

        info = MagicMock(spec=AsyncServiceInfo)
        info.name = name
        info.type = SERVICE_TYPE
        info.port = port
        info.properties = properties
        info.parsed_scoped_addresses.side_effect = lambda ip_version: (
            ["192.168.1.150"] if ip_version.name == "V4Only" else []
        )
        return info

    def test_valid_service_info_parsing(self) -> None:
        info = self._create_mock_service_info()
        device = DiscoveredDevice.from_service_info(info)

        self.assertIsNotNone(device)
        self.assertEqual(device.device_id, "phone-uuid-12345")
        self.assertEqual(device.device_name, "Pixel 9 Pro")
        self.assertEqual(device.device_type, "mobile")
        self.assertEqual(device.os_name, "android")
        self.assertEqual(device.protocol_version, PROTOCOL_VERSION)
        self.assertEqual(device.addresses, ["192.168.1.150"])
        self.assertEqual(device.port, 53770)
        self.assertTrue(device.is_available)

    def test_unsupported_protocol_version_rejected(self) -> None:
        props = {
            b"v": b"99",
            b"id": b"phone-uuid-12345",
            b"name": b"Future Phone",
        }
        info = self._create_mock_service_info(properties=props)
        device = DiscoveredDevice.from_service_info(info)
        self.assertIsNone(device)

    def test_missing_device_id_rejected(self) -> None:
        props = {
            b"v": b"1",
            b"name": b"Pixel 9 Pro",
        }
        info = self._create_mock_service_info(properties=props)
        device = DiscoveredDevice.from_service_info(info)
        self.assertIsNone(device)

    def test_missing_addresses_rejected(self) -> None:
        info = self._create_mock_service_info()
        info.parsed_scoped_addresses.side_effect = lambda ip_version: []
        device = DiscoveredDevice.from_service_info(info)
        self.assertIsNone(device)

    def test_device_to_dict_serialization(self) -> None:
        dev = DiscoveredDevice(
            device_id="d-1",
            device_name="Test Host",
            device_type="desktop",
            os_name="archlinux",
            protocol_version=1,
            addresses=["192.168.1.50"],
            port=53770,
            service_name="Ferry-test._ferry._tcp.local.",
        )
        d = dev.to_dict()
        self.assertEqual(d["device_id"], "d-1")
        self.assertEqual(d["device_name"], "Test Host")
        self.assertEqual(d["is_available"], True)


if __name__ == "__main__":
    unittest.main()
