"""Ephemeral keypair for the local Onirika SSH gateway.

The gateway authenticates Claude Code (and anything else connecting to
127.0.0.1:4242) with a dedicated ed25519 keypair that never leaves this
machine. It has nothing to do with the credentials used to reach the real
remote host — that is the ControlMaster's job. This key only guards the
loopback door.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncssh


def default_key_dir() -> Path:
    """Where the gateway keypair lives: ~/.local/share/onirika/.

    Honours XDG_DATA_HOME when set.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "onirika"


def ensure_keypair(key_dir: Path | None = None) -> tuple[Path, Path]:
    """Return (private_key_path, public_key_path), generating them if absent.

    Idempotent: an existing keypair is reused as-is. The private key is
    written with 0o600 permissions; the directory with 0o700.

    Returns:
        (private_key_path, public_key_path)
    """
    key_dir = key_dir or default_key_dir()
    key_dir.mkdir(parents=True, exist_ok=True)
    # Tighten the directory even if it pre-existed with looser perms.
    os.chmod(key_dir, 0o700)

    priv = key_dir / "proxy_key"
    pub = key_dir / "proxy_key.pub"

    if priv.exists() and pub.exists():
        return priv, pub

    key = asyncssh.generate_private_key("ssh-ed25519")
    # write_private_key / write_public_key emit OpenSSH-format files.
    key.write_private_key(str(priv))
    key.write_public_key(str(pub))

    os.chmod(priv, 0o600)
    os.chmod(pub, 0o644)

    return priv, pub


def load_authorized_key(pub_path: Path) -> asyncssh.SSHKey:
    """Load the gateway public key as an asyncssh key for auth comparison."""
    return asyncssh.read_public_key(str(pub_path))


def ensure_host_key(key_dir: Path | None = None) -> Path:
    """Return the path to the gateway's persistent server host key.

    Unlike the ephemeral default, this key is stable across restarts — which
    matters because Claude Code's SSH client verifies the presented host key
    against ~/.ssh/known_hosts with no trust-on-first-use prompt. A stable key
    lets us register it once and have it keep matching.

    Generates the key on first call; reuses it thereafter. 0o600 perms.
    """
    key_dir = key_dir or default_key_dir()
    key_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(key_dir, 0o700)

    host_key = key_dir / "host_key"
    if host_key.exists():
        return host_key

    key = asyncssh.generate_private_key("ssh-ed25519")
    key.write_private_key(str(host_key))
    os.chmod(host_key, 0o600)
    return host_key


def host_public_line(host_key_path: Path) -> str:
    """Return the host key's public half as a 'ssh-ed25519 AAAA...' string.

    Suitable for composing a ~/.ssh/known_hosts entry (no host prefix, no
    trailing comment).
    """
    key = asyncssh.read_private_key(str(host_key_path))
    # export_public_key yields e.g. b'ssh-ed25519 AAAA... comment\n'
    raw = key.export_public_key("openssh").decode().strip()
    parts = raw.split()
    return f"{parts[0]} {parts[1]}"
