import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from ferry_linux.core.ipc import FerryIPCServer
from ferry_linux.ui.window import FerryMainWindow
from ferry_linux.ui.app import FerryApplication
from ferry_linux.core.session import SessionState, FerrySession
from ferry_linux.core.service import PeerSession, FerryService

class TestUIStatePropagation(unittest.TestCase):
    def test_ipc_session_payload_includes_identity(self):
        # 1. SESSION_UPDATE includes remote_device_id.
        # 2. SESSION_UPDATE includes remote_device_name.
        # 3. SESSION_UPDATE includes remote_static_pub_b64 where available.
        svc_mock = MagicMock(spec=FerryService)
        
        # Mock active session
        session = FerrySession(is_initiator=True)
        session._state = SessionState.ESTABLISHED
        ps = PeerSession(session, None, None, "192.168.1.10:1234")
        ps.remote_device_id = "test-device-uuid"
        ps.remote_device_name = "Test Android"
        ps.remote_static_pub_b64 = "base64-pub-key"
        
        svc_mock._active_sessions = {"192.168.1.10:1234": ps}
        
        ipc_server = FerryIPCServer(svc_mock)
        ipc_server.broadcast = MagicMock()
        
        import asyncio
        original_ensure_future = asyncio.ensure_future
        asyncio.ensure_future = lambda coro: None
        
        try:
            # Act
            ipc_server.on_session_changed("192.168.1.10:1234", SessionState.ESTABLISHED)
            
            # Assert
            ipc_server.broadcast.assert_called_once()
            msg_type, payload = ipc_server.broadcast.call_args[0]
            self.assertEqual(msg_type, "IPC_SESSION_UPDATE")
            self.assertEqual(payload["remote_addr"], "192.168.1.10:1234")
            self.assertEqual(payload["state"], "ESTABLISHED")
            self.assertEqual(payload["remote_device_id"], "test-device-uuid")
            self.assertEqual(payload["remote_device_name"], "Test Android")
            self.assertEqual(payload["remote_static_pub_b64"], "base64-pub-key")
        finally:
            asyncio.ensure_future = original_ensure_future

    def test_gtk_merges_fields_into_session_model(self):
        # 4. GTK merges these fields into its session model.
        window_mock = MagicMock()
        window_mock._ipc_sessions = {}
        # We test the raw function directly, assuming it's unmocked
        from ferry_linux.ui.window import FerryMainWindow
        
        # Bypass Adw.ApplicationWindow init for testing
        class DummyWindow:
            def __init__(self):
                self._ipc_sessions = {}
            handle_session_state_ipc = FerryMainWindow.handle_session_state_ipc
            handle_session_state = MagicMock()
            
        win = DummyWindow()
        
        payload = {
            "remote_addr": "192.168.1.10:1234",
            "state": "ESTABLISHED",
            "remote_device_id": "test-device-uuid",
            "remote_device_name": "Test Android",
            "remote_static_pub_b64": "base64-pub-key",
        }
        
        import gi
        gi.require_version('GLib', '2.0')
        from gi.repository import GLib
        
        # Patch GLib.idle_add to run synchronously for test
        old_idle_add = GLib.idle_add
        GLib.idle_add = lambda f, *a, **k: f(*a, **k)
        
        try:
            win.handle_session_state_ipc(payload)
            
            # Assert
            session_model = win._ipc_sessions["192.168.1.10:1234"]
            self.assertEqual(session_model["remote_addr"], "192.168.1.10:1234")
            self.assertEqual(session_model["state"], "ESTABLISHED")
            self.assertEqual(session_model["remote_device_id"], "test-device-uuid")
            self.assertEqual(session_model["remote_device_name"], "Test Android")
            self.assertEqual(session_model["remote_static_pub_b64"], "base64-pub-key")
        finally:
            GLib.idle_add = old_idle_add

if __name__ == '__main__':
    unittest.main()
