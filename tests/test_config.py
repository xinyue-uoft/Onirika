"""Tests for configuration loading and validation."""

import pytest
import tempfile
from pathlib import Path

from onirika.config import load_config, HostConfig, ServerConfig


def _write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(content)
    return p


class TestHostConfig:
    def test_ssh_target_with_user(self):
        h = HostConfig(ssh_host="server.example.com", user="alice")
        assert h.ssh_target == "alice@server.example.com"

    def test_ssh_target_without_user(self):
        h = HostConfig(ssh_host="server.example.com")
        assert h.ssh_target == "server.example.com"

    def test_resolved_control_path_expands_tokens(self):
        h = HostConfig(ssh_host="myhost", user="bob", port=2222,
                       control_path="/tmp/ctrl-%r@%h:%p")
        assert h.resolved_control_path == "/tmp/ctrl-bob@myhost:2222"

    def test_defaults(self):
        h = HostConfig(ssh_host="x")
        assert h.port == 22
        assert h.command_timeout == 30
        assert h.file_timeout == 60
        assert h.max_output_lines == 5000
        assert h.default_cwd == "~"


class TestServerConfig:
    def test_get_host_by_name(self):
        cfg = ServerConfig(
            default_host="a",
            hosts={"a": HostConfig(ssh_host="a.com"), "b": HostConfig(ssh_host="b.com")},
        )
        assert cfg.get_host("b").ssh_host == "b.com"

    def test_get_host_default(self):
        cfg = ServerConfig(
            default_host="a",
            hosts={"a": HostConfig(ssh_host="a.com")},
        )
        assert cfg.get_host().ssh_host == "a.com"

    def test_get_host_single_no_default(self):
        cfg = ServerConfig(hosts={"only": HostConfig(ssh_host="only.com")})
        assert cfg.get_host().ssh_host == "only.com"

    def test_get_host_unknown_raises(self):
        cfg = ServerConfig(hosts={"a": HostConfig(ssh_host="a.com")})
        with pytest.raises(ValueError, match="Unknown host 'nope'"):
            cfg.get_host("nope")

    def test_get_host_ambiguous_no_default_raises(self):
        cfg = ServerConfig(
            hosts={"a": HostConfig(ssh_host="a.com"), "b": HostConfig(ssh_host="b.com")},
        )
        with pytest.raises(ValueError, match="No host specified"):
            cfg.get_host()


class TestLoadConfig:
    def test_valid_config(self, tmp_path):
        p = _write_config(tmp_path, """
default_host: myserver
hosts:
  myserver:
    ssh_host: server.example.com
    user: alice
    command_timeout: 60
""")
        cfg = load_config(p)
        assert cfg.default_host == "myserver"
        assert cfg.hosts["myserver"].ssh_host == "server.example.com"
        assert cfg.hosts["myserver"].user == "alice"
        assert cfg.hosts["myserver"].command_timeout == 60

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_empty_hosts_raises(self, tmp_path):
        p = _write_config(tmp_path, "default_host: x\nhosts: {}")
        with pytest.raises(ValueError, match="at least one host"):
            load_config(p)

    def test_missing_ssh_host_raises(self, tmp_path):
        p = _write_config(tmp_path, """
hosts:
  bad:
    user: alice
""")
        with pytest.raises(ValueError, match="missing required field 'ssh_host'"):
            load_config(p)

    def test_unknown_field_raises(self, tmp_path):
        p = _write_config(tmp_path, """
hosts:
  srv:
    ssh_host: example.com
    bogus_field: 123
""")
        with pytest.raises(ValueError, match="unknown fields.*bogus_field"):
            load_config(p)

    def test_invalid_default_host_raises(self, tmp_path):
        p = _write_config(tmp_path, """
default_host: missing
hosts:
  actual:
    ssh_host: example.com
""")
        with pytest.raises(ValueError, match="default_host 'missing' not found"):
            load_config(p)

    def test_preamble(self, tmp_path):
        p = _write_config(tmp_path, """
hosts:
  srv:
    ssh_host: example.com
    preamble: |
      source /opt/env.sh
      module load gcc
""")
        cfg = load_config(p)
        assert "source /opt/env.sh" in cfg.hosts["srv"].preamble
        assert "module load gcc" in cfg.hosts["srv"].preamble
