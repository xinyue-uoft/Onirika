"""Configuration loading and dataclasses for Onirika SSH MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class HostConfig:
    """Configuration for a single SSH host."""

    ssh_host: str
    user: str | None = None
    port: int = 22
    control_path: str = "~/.ssh/ctrl-%r@%h:%p"
    preamble: str = ""
    default_cwd: str = "~"
    command_timeout: int = 30
    file_timeout: int = 60
    max_file_size: int = 10 * 1024 * 1024  # 10MB
    max_output_lines: int = 5000

    @property
    def resolved_control_path(self) -> str:
        """Expand ~ and SSH tokens in control_path."""
        path = os.path.expanduser(self.control_path)
        # Resolve SSH-style tokens for the check command
        path = path.replace("%h", self.ssh_host)
        if self.user:
            path = path.replace("%r", self.user)
        path = path.replace("%p", str(self.port))
        return path

    @property
    def ssh_target(self) -> str:
        """The SSH target string (user@host or just host)."""
        if self.user:
            return f"{self.user}@{self.ssh_host}"
        return self.ssh_host


@dataclass
class ServerConfig:
    """Top-level server configuration."""

    default_host: str = ""
    hosts: dict[str, HostConfig] = field(default_factory=dict)

    def get_host(self, name: str | None = None) -> HostConfig:
        """Get a host config by name, falling back to default."""
        name = name or self.default_host
        if not name:
            if len(self.hosts) == 1:
                return next(iter(self.hosts.values()))
            raise ValueError(
                "No host specified and no default_host configured. "
                f"Available hosts: {', '.join(self.hosts.keys())}"
            )
        if name not in self.hosts:
            raise ValueError(
                f"Unknown host '{name}'. "
                f"Available hosts: {', '.join(self.hosts.keys())}"
            )
        return self.hosts[name]


def _default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "onirika" / "config.yaml"


def load_config(path: str | Path | None = None) -> ServerConfig:
    """Load configuration from YAML file.

    Resolution order:
    1. Explicit path argument
    2. ONIRIKA_CONFIG environment variable
    3. ~/.config/onirika/config.yaml
    """
    if path is None:
        path = os.environ.get("ONIRIKA_CONFIG")
    if path is None:
        path = _default_config_path()

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Create one at {_default_config_path()} or pass --config <path>.\n"
            f"See config.example.yaml for the format."
        )

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Config file must be a YAML mapping, got {type(raw).__name__}")

    raw_hosts = raw.get("hosts", {})
    if not raw_hosts:
        raise ValueError(
            "Config must define at least one host under 'hosts:'.\n"
            "See config.example.yaml for the format."
        )

    hosts = {}
    for name, host_raw in raw_hosts.items():
        if not isinstance(host_raw, dict):
            raise ValueError(f"Host '{name}' must be a YAML mapping")
        if "ssh_host" not in host_raw:
            raise ValueError(f"Host '{name}' is missing required field 'ssh_host'")
        # Filter out unknown keys to avoid TypeError on HostConfig init
        known_fields = {f.name for f in HostConfig.__dataclass_fields__.values()}
        unknown = set(host_raw.keys()) - known_fields
        if unknown:
            raise ValueError(
                f"Host '{name}' has unknown fields: {', '.join(sorted(unknown))}. "
                f"Valid fields: {', '.join(sorted(known_fields))}"
            )
        hosts[name] = HostConfig(**host_raw)

    default_host = raw.get("default_host", "")
    if default_host and default_host not in hosts:
        raise ValueError(
            f"default_host '{default_host}' not found in hosts. "
            f"Available: {', '.join(hosts.keys())}"
        )

    return ServerConfig(default_host=default_host, hosts=hosts)


# Cached config singleton
_config: ServerConfig | None = None


def get_config() -> ServerConfig:
    """Get the cached server configuration, loading on first access."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
