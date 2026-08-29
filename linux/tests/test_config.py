"""
Unit tests for Ferry Configuration Manager.
"""

import tempfile
import unittest
from pathlib import Path

from ferry_linux.core.config import ConfigManager, FerryConfig


class TestConfigManager(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.config_manager = ConfigManager(base_dir=self.base_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_default_config_creation(self) -> None:
        config = self.config_manager.load_config()
        self.assertIsInstance(config, FerryConfig)
        self.assertEqual(config.listen_port, 53770)
        self.assertTrue(self.config_manager.config_file.exists())

    def test_save_and_reload_config(self) -> None:
        config = self.config_manager.load_config()
        config.device_name = "Custom Test Host"
        config.listen_port = 59999
        config.auto_accept_paired = True

        self.config_manager.save_config(config)

        # Reload with fresh manager pointing to same directory
        reloaded_mgr = ConfigManager(base_dir=self.base_path)
        loaded = reloaded_mgr.load_config()

        self.assertEqual(loaded.device_name, "Custom Test Host")
        self.assertEqual(loaded.listen_port, 59999)
        self.assertTrue(loaded.auto_accept_paired)


if __name__ == "__main__":
    unittest.main()
