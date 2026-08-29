"""
Ferry Local Discovery Subsystem using AsyncZeroconf (mDNS / DNS-SD).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import (
    AsyncServiceBrowser,
    AsyncServiceInfo,
    AsyncZeroconf,
)

from .config import FerryConfig

logger = logging.getLogger("ferry.discovery")

SERVICE_TYPE = "_ferry._tcp.local."
PROTOCOL_VERSION = 1


@dataclass
class DiscoveredDevice:
    """Represents a peer discovered on the local network (UNTRUSTED)."""
    device_id: str
    device_name: str
    device_type: str
    os_name: str
    protocol_version: int
    addresses: List[str]
    port: int
    service_name: str
    last_seen: float = field(default_factory=time.time)
    is_available: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_service_info(cls, info: AsyncServiceInfo) -> Optional[DiscoveredDevice]:
        """Parse DNS-SD TXT properties and socket addresses into DiscoveredDevice."""
        props = info.properties
        if not props:
            return None

        def get_prop(key: bytes, default: str = "") -> str:
            val = props.get(key)
            if val is None:
                return default
            return val.decode("utf-8", errors="replace")

        try:
            proto_ver = int(get_prop(b"v", "1"))
        except ValueError:
            return None

        # Ignore incompatible protocol versions
        if proto_ver != PROTOCOL_VERSION:
            logger.warning("Discovered service with unsupported protocol version: %d", proto_ver)
            return None

        device_id = get_prop(b"id")
        if not device_id:
            return None

        device_name = get_prop(b"name", "Unknown Ferry Peer")
        device_type = get_prop(b"type", "unknown")
        os_name = get_prop(b"os", "unknown")

        addresses: List[str] = []
        for addr in info.parsed_scoped_addresses(IPVersion.V4Only):
            if addr not in addresses and not addr.startswith("127."):
                addresses.append(addr)
        for addr in info.parsed_scoped_addresses(IPVersion.V6Only):
            if addr not in addresses and not addr.startswith("::1"):
                addresses.append(addr)

        if not addresses:
            return None

        return cls(
            device_id=device_id,
            device_name=device_name,
            device_type=device_type,
            os_name=os_name,
            protocol_version=proto_ver,
            addresses=addresses,
            port=info.port,
            service_name=info.name,
            last_seen=time.time(),
            is_available=True,
        )


def get_local_ip_addresses() -> list[bytes]:
    """Collect packed byte representations of local non-loopback IPv4 addresses."""
    addrs: list[bytes] = []
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip_info in adapter.ips:
                if ip_info.is_IPv4 and not ip_info.ip.startswith("127.") and not ip_info.ip.startswith("172.17."):
                    packed = socket.inet_aton(ip_info.ip)
                    if packed not in addrs:
                        addrs.append(packed)
    except Exception:
        pass

    if not addrs:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            addrs.append(socket.inet_aton(s.getsockname()[0]))
            s.close()
        except Exception:
            pass

    return addrs


class DiscoveryManager:
    """Manages mDNS advertisement and discovery of peer Ferry instances."""

    def __init__(self, config: FerryConfig) -> None:
        self.config = config
        self.azc: Optional[AsyncZeroconf] = None
        self.service_info: Optional[AsyncServiceInfo] = None
        self.browser: Optional[AsyncServiceBrowser] = None
        self._devices: Dict[str, DiscoveredDevice] = {}  # device_id -> DiscoveredDevice
        self._service_to_device: Dict[str, str] = {}    # service_name -> device_id
        self._listeners: Set[Callable[[List[DiscoveredDevice]], None]] = set()
        self._is_running = False
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def add_listener(self, callback: Callable[[List[DiscoveredDevice]], None]) -> None:
        """Register a callback for discovered device list updates."""
        self._listeners.add(callback)
        # Immediately notify with current state
        callback(self.get_devices())

    def remove_listener(self, callback: Callable[[List[DiscoveredDevice]], None]) -> None:
        """Unregister a device update callback."""
        self._listeners.discard(callback)

    def _notify_listeners(self) -> None:
        devices = self.get_devices()
        for callback in list(self._listeners):
            try:
                callback(devices)
            except Exception as e:
                logger.error("Error in discovery listener callback: %s", e)

    def get_devices(self) -> List[DiscoveredDevice]:
        """Return all currently available discovered peers."""
        return [d for d in self._devices.values() if d.is_available]

    def get_device(self, device_id: str) -> Optional[DiscoveredDevice]:
        """Get a discovered peer by its unique device ID."""
        dev = self._devices.get(device_id)
        if dev and dev.is_available:
            return dev
        return None

    async def start(self) -> None:
        """Start mDNS advertisement and browsing."""
        async with self._lock:
            if self._is_running:
                return

            logger.info("Initializing AsyncZeroconf for Ferry discovery...")
            self.azc = AsyncZeroconf()

            # 1. Register own service advertisement
            instance_id = self.config.device_id[:8] if self.config.device_id else "host"
            service_name = f"Ferry-{instance_id}.{SERVICE_TYPE}"

            properties = {
                b"v": str(PROTOCOL_VERSION).encode("utf-8"),
                b"id": self.config.device_id.encode("utf-8"),
                b"name": self.config.device_name.encode("utf-8"),
                b"type": b"desktop",
                b"os": b"archlinux",
                b"port": str(self.config.listen_port).encode("utf-8"),
                b"app_version": b"0.1.0",
            }

            local_addrs = get_local_ip_addresses()
            self.service_info = AsyncServiceInfo(
                type_=SERVICE_TYPE,
                name=service_name,
                port=self.config.listen_port,
                properties=properties,
                addresses=local_addrs if local_addrs else [socket.inet_aton("127.0.0.1")],
                server=f"{socket.gethostname()}.local.",
            )

            await self.azc.async_register_service(self.service_info)
            logger.info("Ferry mDNS service registered as: %s with IP(s): %s",
                        service_name, [socket.inet_ntoa(a) for a in self.service_info.addresses])

            # 2. Start browsing for peer services
            self.browser = AsyncServiceBrowser(
                self.azc.zeroconf,
                SERVICE_TYPE,
                handlers=[self._on_service_state_change],
            )
            self._is_running = True
            logger.info("Ferry mDNS service browser active for %s", SERVICE_TYPE)

    def _on_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Dispatch service state change event into the asyncio loop."""
        asyncio.create_task(self._async_handle_state_change(service_type, name, state_change))

    async def _async_handle_state_change(
        self,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Handle service addition, update, or removal."""
        if not self.azc or not self._is_running:
            return

        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            info = AsyncServiceInfo(service_type, name)
            if await info.async_request(self.azc.zeroconf, 3000):
                device = DiscoveredDevice.from_service_info(info)
                if not device:
                    return

                # Ignore our own advertisement
                if device.device_id == self.config.device_id:
                    return

                async with self._lock:
                    self._devices[device.device_id] = device
                    self._service_to_device[name] = device.device_id
                    logger.info("Discovered/Updated Ferry peer: %s (%s, %s) at %s:%d",
                                device.device_name, device.device_type, device.os_name,
                                device.addresses[0] if device.addresses else "no-ip", device.port)
                    self._notify_listeners()

        elif state_change == ServiceStateChange.Removed:
            async with self._lock:
                device_id = self._service_to_device.pop(name, None)
                if device_id and device_id in self._devices:
                    self._devices[device_id].is_available = False
                    logger.info("Ferry peer removed: %s (service: %s)", self._devices[device_id].device_name, name)
                    self._notify_listeners()

    async def stop(self) -> None:
        """Cleanly stop advertisement and browsing."""
        async with self._lock:
            if not self._is_running:
                return

            logger.info("Stopping Ferry discovery manager...")
            self._is_running = False

            if self.browser:
                await self.browser.async_cancel()
                self.browser = None

            if self.azc and self.service_info:
                try:
                    await self.azc.async_unregister_service(self.service_info)
                except Exception as e:
                    logger.warning("Error unregistering service: %s", e)
                self.service_info = None

            if self.azc:
                await self.azc.async_close()
                self.azc = None

            self._devices.clear()
            self._service_to_device.clear()
            self._notify_listeners()
            logger.info("Ferry discovery manager stopped.")
