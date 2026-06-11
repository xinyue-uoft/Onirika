"""Tests for the gateway keypair generation/reuse."""

import os
import stat

from onirika import proxy_key


def test_generates_keypair(tmp_path):
    priv, pub = proxy_key.ensure_keypair(tmp_path / "onirika")
    assert priv.exists()
    assert pub.exists()
    assert priv.read_text().strip() != ""
    assert pub.read_text().startswith("ssh-ed25519 ")


def test_idempotent_reuses_existing(tmp_path):
    key_dir = tmp_path / "onirika"
    priv1, pub1 = proxy_key.ensure_keypair(key_dir)
    priv_bytes = priv1.read_bytes()
    pub_bytes = pub1.read_bytes()

    priv2, pub2 = proxy_key.ensure_keypair(key_dir)
    assert priv2 == priv1
    assert pub2 == pub1
    # Content untouched on the second call.
    assert priv2.read_bytes() == priv_bytes
    assert pub2.read_bytes() == pub_bytes


def test_private_key_permissions(tmp_path):
    priv, _ = proxy_key.ensure_keypair(tmp_path / "onirika")
    mode = stat.S_IMODE(os.stat(priv).st_mode)
    assert mode == 0o600


def test_key_dir_permissions(tmp_path):
    key_dir = tmp_path / "onirika"
    proxy_key.ensure_keypair(key_dir)
    mode = stat.S_IMODE(os.stat(key_dir).st_mode)
    assert mode == 0o700


def test_loadable_as_authorized_key(tmp_path):
    _, pub = proxy_key.ensure_keypair(tmp_path / "onirika")
    key = proxy_key.load_authorized_key(pub)
    assert key is not None


def test_host_key_generated_and_persistent(tmp_path):
    key_dir = tmp_path / "onirika"
    hk1 = proxy_key.ensure_host_key(key_dir)
    assert hk1.exists()
    content = hk1.read_bytes()
    hk2 = proxy_key.ensure_host_key(key_dir)
    assert hk2 == hk1
    assert hk2.read_bytes() == content  # not regenerated


def test_host_key_permissions(tmp_path):
    hk = proxy_key.ensure_host_key(tmp_path / "onirika")
    mode = stat.S_IMODE(os.stat(hk).st_mode)
    assert mode == 0o600


def test_host_public_line_format(tmp_path):
    hk = proxy_key.ensure_host_key(tmp_path / "onirika")
    line = proxy_key.host_public_line(hk)
    parts = line.split()
    assert len(parts) == 2
    assert parts[0] == "ssh-ed25519"
    assert parts[1].startswith("AAAA")
