"""Tests for ~/.ssh/known_hosts registration of the gateway host key."""

from onirika import known_hosts
from onirika.ssh_config import ALIAS

SAMPLE = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAISAMPLEKEYDATA1234567890abcdef"


def test_register_creates_file(tmp_path):
    kh = tmp_path / ".ssh" / "known_hosts"
    known_hosts.register(SAMPLE, 4242, path=kh)
    assert kh.exists()
    text = kh.read_text()
    assert SAMPLE in text
    assert known_hosts.MARKER in text


def test_register_covers_all_host_forms(tmp_path):
    kh = tmp_path / "known_hosts"
    known_hosts.register(SAMPLE, 4242, path=kh)
    line = kh.read_text().strip()
    assert ALIAS in line
    assert "[127.0.0.1]:4242" in line
    assert f"[{ALIAS}]:4242" in line


def test_register_is_idempotent(tmp_path):
    kh = tmp_path / "known_hosts"
    known_hosts.register(SAMPLE, 4242, path=kh)
    first = kh.read_text()
    known_hosts.register(SAMPLE, 4242, path=kh)
    second = kh.read_text()
    assert first == second
    assert second.count(known_hosts.MARKER) == 1


def test_register_preserves_other_entries(tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("github.com ssh-ed25519 AAAAsomeexistingkey\n")
    known_hosts.register(SAMPLE, 4242, path=kh)
    text = kh.read_text()
    assert "github.com" in text
    assert known_hosts.MARKER in text


def test_deregister_removes_only_managed(tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("github.com ssh-ed25519 AAAAsomeexistingkey\n")
    known_hosts.register(SAMPLE, 4242, path=kh)
    assert known_hosts.is_registered(kh)
    known_hosts.deregister(kh)
    text = kh.read_text()
    assert "github.com" in text
    assert known_hosts.MARKER not in text
    assert not known_hosts.is_registered(kh)


def test_deregister_no_file(tmp_path):
    assert known_hosts.deregister(tmp_path / "nope") is False


def test_deregister_no_managed_lines(tmp_path):
    kh = tmp_path / "known_hosts"
    kh.write_text("github.com ssh-ed25519 AAAAkey\n")
    assert known_hosts.deregister(kh) is False
    assert "github.com" in kh.read_text()


def test_reregister_updates_key(tmp_path):
    kh = tmp_path / "known_hosts"
    known_hosts.register(SAMPLE, 4242, path=kh)
    new_key = "ssh-ed25519 AAAADIFFERENTKEYVALUE9876543210"
    known_hosts.register(new_key, 4242, path=kh)
    text = kh.read_text()
    assert new_key in text
    assert SAMPLE not in text
    assert text.count(known_hosts.MARKER) == 1
