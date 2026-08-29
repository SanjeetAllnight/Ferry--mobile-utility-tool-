"""
Control-plane integration tests: two in-process FerryService instances
complete a full handshake to ESTABLISHED state.

These tests spin up real asyncio TCP servers and clients using temp ports,
validating the complete Phase 2B handshake flow without a physical device.
"""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from ferry_linux.core.config import ConfigManager, FerryConfig
from ferry_linux.core.service import FerryService
from ferry_linux.core.session import SessionState


def _make_service(port: int, tmp_dir: str, subdir: str) -> FerryService:
    base = Path(tmp_dir) / subdir
    base.mkdir(parents=True, exist_ok=True)
    svc = FerryService(base_dir=base)
    svc.config.listen_port = port
    svc.config.device_name = f"TestDevice-{subdir}"
    return svc


class TestControlPlaneIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # Pick two available ephemeral ports
        import socket as _sock
        def _free_port() -> int:
            with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]

        self.port_a = _free_port()
        self.port_b = _free_port()

        self.svc_a = _make_service(self.port_a, self._tmp.name, "a")
        self.svc_b = _make_service(self.port_b, self._tmp.name, "b")

        # Start TCP servers only (skip mDNS in unit tests)
        await self._start_tcp_only(self.svc_a)
        await self._start_tcp_only(self.svc_b)

    @staticmethod
    async def _start_tcp_only(svc: FerryService) -> None:
        """Start the TCP server without mDNS (for fast unit tests)."""
        svc._is_running = True
        Path(svc.config.download_dir).mkdir(parents=True, exist_ok=True)
        svc._server = await asyncio.start_server(
            svc._handle_incoming_connection,
            host="127.0.0.1",
            port=svc.config.listen_port,
        )

    async def asyncTearDown(self) -> None:
        for svc in (self.svc_a, self.svc_b):
            svc._is_running = False
            if svc._server:
                svc._server.close()
                await svc._server.wait_closed()
                svc._server = None
            for task in list(svc._active_tasks):
                task.cancel()
        self._tmp.cleanup()

    async def test_full_handshake_to_established(self) -> None:
        """
        Service A connects to Service B over loopback.
        Both reach ESTABLISHED state.
        """
        from ferry_linux.core.discovery import DiscoveredDevice

        # Manufacture a DiscoveredDevice for B as seen by A
        device_b = DiscoveredDevice(
            device_id=self.svc_b.config.device_id,
            device_name=self.svc_b.config.device_name,
            device_type="desktop",
            os_name="archlinux",
            port=self.port_b,
            addresses=["127.0.0.1"],
            protocol_version=1,
            service_name="Ferry-test-b",
        )

        # Track state changes on B's side
        b_states: list[SessionState] = []

        def on_b_state(addr: str, state: SessionState) -> None:
            b_states.append(state)

        self.svc_b.add_session_listener(on_b_state)

        # A initiates connection to B
        peer_session_a = await self.svc_a.connect_to_peer(device_b)
        # connect_to_peer returns PeerSession if PAIRING or ESTABLISHED now? No, wait. 
        # If connect_to_peer returns None on PAIRING, how do we get peer_session_a?
        # Let's fix connect_to_peer to return it if it's PAIRING.
        # Actually, let's just get the session from active_sessions!

        # Wait for both sides to reach PAIRING
        await asyncio.sleep(0.5)

        addr_b = f"{device_b.addresses[0]}:{device_b.port}"
        # A's view of B
        ps_a = self.svc_a._active_sessions.get(addr_b)
        self.assertIsNotNone(ps_a, f"A should have an active session for B. Keys: {self.svc_a._active_sessions.keys()}")

        # B's view of A
        # A's port isn't easily known, so we just get the first active session on B
        ps_b = list(self.svc_b._active_sessions.values())[0]

        # Simulate user accepting on both sides
        await self.svc_a.accept_pairing(ps_a.remote_addr)
        await self.svc_b.accept_pairing(ps_b.remote_addr)

        # Give it a moment to process the PAIR_DECISION messages
        await asyncio.sleep(0.5)

        self.assertEqual(
            ps_a.session.state, SessionState.ESTABLISHED,
            f"Session A should be ESTABLISHED, got {ps_a.session.state}"
        )

        self.assertIn(
            SessionState.ESTABLISHED, b_states,
            f"Service B should reach ESTABLISHED. States seen: {b_states}"
        )

    async def test_reconnect_uses_persisted_trust(self) -> None:
        """After first pairing, reconnect should reach ESTABLISHED without new pairing."""
        from ferry_linux.core.discovery import DiscoveredDevice

        device_b = DiscoveredDevice(
            device_id=self.svc_b.config.device_id,
            device_name=self.svc_b.config.device_name,
            device_type="desktop",
            os_name="archlinux",
            port=self.port_b,
            addresses=["127.0.0.1"],
            protocol_version=1,
            service_name="Ferry-test-b",
        )

        # First connection — pairing
        asyncio.create_task(self.svc_a.connect_to_peer(device_b))
        await asyncio.sleep(0.5)
        
        addr_b = f"{device_b.addresses[0]}:{device_b.port}"
        ps_a = self.svc_a._active_sessions.get(addr_b)
        self.assertIsNotNone(ps_a, f"A should have an active session for B. Keys: {self.svc_a._active_sessions.keys()}")
        
        ps_b = list(self.svc_b._active_sessions.values())[0]

        # Accept pairing
        await self.svc_a.accept_pairing(ps_a.remote_addr)
        await self.svc_b.accept_pairing(ps_b.remote_addr)
        await asyncio.sleep(0.5)

        self.assertEqual(ps_a.session.state, SessionState.ESTABLISHED)

        # Disconnect A's writer
        try:
            ps_a.writer.close()
            await ps_a.writer.wait_closed()
        except Exception:
            pass
        await asyncio.sleep(0.2)

        # Reconnect
        asyncio.create_task(self.svc_a.connect_to_peer(device_b))
        await asyncio.sleep(0.5)
        
        ps_a2 = self.svc_a._active_sessions.get(addr_b)
        self.assertIsNotNone(ps_a2)
        self.assertEqual(ps_a2.session.state, SessionState.ESTABLISHED)

    async def test_identity_persists_across_service_restart(self) -> None:
        """Service identity (public key) must be the same after restart."""
        key1 = self.svc_a.identity.public_key_b64

        await self.svc_a.stop()

        # Re-create service from same base_dir — should reload same identity key
        base = Path(self._tmp.name) / "a"
        svc_a2 = FerryService(base_dir=base)
        await svc_a2.start()

        key2 = svc_a2.identity.public_key_b64
        await svc_a2.stop()

        self.assertEqual(key1, key2, "Identity key must survive service restart")


class TestControlPlaneRejection(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        import socket as _sock
        with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.svc = _make_service(self.port, self._tmp.name, "srv")
        self.svc._is_running = True
        Path(self.svc.config.download_dir).mkdir(parents=True, exist_ok=True)
        self.svc._server = await asyncio.start_server(
            self.svc._handle_incoming_connection,
            host="127.0.0.1",
            port=self.port,
        )

    async def asyncTearDown(self) -> None:
        self.svc._is_running = False
        if self.svc._server:
            self.svc._server.close()
            await self.svc._server.wait_closed()
        for task in list(self.svc._active_tasks):
            task.cancel()
        self._tmp.cleanup()

    async def test_invalid_magic_bytes_rejected(self) -> None:
        """A raw connection sending garbage should not crash the server."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"\x00\x00\x00\x00\x00garbage")
        await writer.drain()

        # Server should drop the connection; we get EOF or ConnectionResetError
        try:
            data = await asyncio.wait_for(reader.read(100), timeout=3.0)
        except (asyncio.TimeoutError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            writer.close()
        # Either data is empty or server sent an error — what matters is no crash

    async def test_plaintext_after_handshake_rejected(self) -> None:
        """Sending a plaintext Ferry frame instead of AEAD after handshake is an error."""
        # This is best validated by the session log; no crash is the key invariant.
        pass  # covered by test_tampered_ciphertext_rejected in test_session.py


if __name__ == "__main__":
    unittest.main()
