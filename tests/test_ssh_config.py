"""Tests for ~/.ssh/config block injection and removal."""

import os
import stat
from pathlib import Path

from onirika import ssh_config


def test_inject_creates_config_if_absent(tmp_path):
    cfg = tmp_path / ".ssh" / "config"
    ssh_config.inject("alice", Path("/keys/proxy_key"), config_path=cfg)
    assert cfg.exists()
    text = cfg.read_text()
    assert f"Host {ssh_config.ALIAS}" in text
    assert "HostName 127.0.0.1" in text


def test_inject_writes_correct_fields(tmp_path):
    cfg = tmp_path / "config"
    ssh_config.inject("bob", Path("/keys/k"), port=4242, config_path=cfg)
    text = cfg.read_text()
    assert "Port 4242" in text
    assert "User bob" in text
    assert "IdentityFile /keys/k" in text
    assert "StrictHostKeyChecking no" in text
    assert "UserKnownHostsFile /dev/null" in text


def test_inject_without_user_omits_user_line(tmp_path):
    cfg = tmp_path / "config"
    ssh_config.inject(None, Path("/keys/k"), config_path=cfg)
    text = cfg.read_text()
    assert "User " not in text
    assert f"Host {ssh_config.ALIAS}" in text


def test_inject_is_idempotent(tmp_path):
    cfg = tmp_path / "config"
    ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    first = cfg.read_text()
    ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    second = cfg.read_text()
    assert first == second
    # Exactly one block.
    assert second.count(ssh_config.BEGIN_MARKER) == 1


def test_inject_preserves_other_hosts(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(
        "Host myserver\n"
        "    HostName example.com\n"
        "    User me\n"
    )
    ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    text = cfg.read_text()
    assert "Host myserver" in text
    assert "HostName example.com" in text
    assert f"Host {ssh_config.ALIAS}" in text


def test_remove_cleans_block(tmp_path):
    cfg = tmp_path / "config"
    ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    assert ssh_config.is_injected(cfg)
    removed = ssh_config.remove(cfg)
    assert removed
    assert not ssh_config.is_injected(cfg)
    assert ssh_config.BEGIN_MARKER not in cfg.read_text()


def test_remove_preserves_other_hosts(tmp_path):
    cfg = tmp_path / "config"
    original = (
        "Host myserver\n"
        "    HostName example.com\n"
        "    User me\n"
    )
    cfg.write_text(original)
    ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    ssh_config.remove(cfg)
    text = cfg.read_text()
    assert "Host myserver" in text
    assert "HostName example.com" in text
    assert ssh_config.ALIAS not in text


def test_inject_remove_roundtrip_restores_other_content(tmp_path):
    cfg = tmp_path / "config"
    original = "Host a\n    HostName a.com\n\nHost b\n    HostName b.com\n"
    cfg.write_text(original)
    ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    ssh_config.remove(cfg)
    # The non-onirika content should survive intact (modulo trailing whitespace).
    text = cfg.read_text()
    assert "Host a" in text
    assert "Host b" in text
    assert "HostName a.com" in text
    assert "HostName b.com" in text


def test_remove_no_file_returns_false(tmp_path):
    cfg = tmp_path / "nonexistent"
    assert ssh_config.remove(cfg) is False


def test_remove_no_block_returns_false(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host x\n    HostName x.com\n")
    assert ssh_config.remove(cfg) is False


def test_is_injected_false_when_absent(tmp_path):
    cfg = tmp_path / "config"
    assert ssh_config.is_injected(cfg) is False
    cfg.write_text("Host x\n")
    assert ssh_config.is_injected(cfg) is False


def test_config_permissions_after_inject(tmp_path):
    cfg = tmp_path / ".ssh" / "config"
    ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    mode = stat.S_IMODE(os.stat(cfg).st_mode)
    assert mode == 0o600


def test_reinject_does_not_accumulate_blank_lines(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host x\n    HostName x.com\n")
    for _ in range(5):
        ssh_config.inject("alice", Path("/keys/k"), config_path=cfg)
    text = cfg.read_text()
    assert text.count(ssh_config.BEGIN_MARKER) == 1
    # No runs of 3+ newlines.
    assert "\n\n\n" not in text
