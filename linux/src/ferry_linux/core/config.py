"""
Configuration and XDG Directory Manager for Ferry.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict


import uuid


@dataclass
class FerryConfig:
    """Ferry configuration settings."""
    device_name: str = field(default_factory=lambda: f"{socket.gethostname()} (Ferry)")
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    listen_port: int = 53770
    download_dir: str = field(default_factory=lambda: str(Path.home() / "Downloads" / "Ferry"))
    auto_accept_paired: bool = False
    max_chunk_size: int = 65536  # 64 KiB

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> FerryConfig:
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ConfigManager:
    """Manages reading, writing, and directory initialization for Ferry."""

    def __init__(self, base_dir: Path | None = None) -> None:
        if base_dir:
            self.config_dir = base_dir / "config"
            self.data_dir = base_dir / "data"
            self.runtime_dir = base_dir / "runtime"
        else:
            xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
            xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
            xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

            self.config_dir = Path(xdg_config) / "ferry"
            self.data_dir = Path(xdg_data) / "ferry"
            self.runtime_dir = Path(xdg_runtime) / "ferry"

        self.config_file = self.config_dir / "config.json"
        self.db_file = self.data_dir / "ferry.db"
        self.ipc_socket_file = self.runtime_dir / "ferry.sock"

        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """Create necessary directories if they do not exist."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Fallback if runtime dir permissions prevent creation
            self.runtime_dir = self.data_dir / "runtime"
            self.ipc_socket_file = self.runtime_dir / "ferry.sock"
            self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> FerryConfig:
        """Load configuration from disk or create default."""
        if not self.config_file.exists():
            config = FerryConfig()
            self.save_config(config)
            return config

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = FerryConfig.from_dict(data)
                if not config.device_id:
                    config.device_id = str(uuid.uuid4())
                    self.save_config(config)
                return config
        except Exception:
            config = FerryConfig()
            self.save_config(config)
            return config

    def save_config(self, config: FerryConfig) -> None:
        """Save configuration to disk."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(config.to_dict(), f, indent=2)
