"""Register the gateway's host key in ~/.ssh/known_hosts.

Claude Code's SSH client verifies the presented host key against
``~/.ssh/known_hosts`` directly — it ignores ``StrictHostKeyChecking`` and
``UserKnownHostsFile`` from ssh_config, and offers no trust-on-first-use prompt.
So for Claude Code to accept the gateway, the gateway's (persistent) host key
must already be present in known_hosts before it connects.

We don't know exactly which host string Claude Code uses for the lookup, so we
register the key under every plausible form — the bare alias, and the
``[127.0.0.1]:<port>`` / ``[alias]:<port>`` variants. Extra entries are
harmless. Every managed line carries a trailing ``onirika-gateway`` comment so
removal is surgical.
"""

from __future__ import annotations

import os
from pathlib import Path

from onirika.ssh_config import ALIAS

MARKER = "onirika-gateway"


def _default_path() -> Path:
    return Path.home() / ".ssh" / "known_hosts"


def _host_patterns(port: int) -> str:
    """Comma-joined host forms a client might key the lookup on."""
    forms = [
        ALIAS,
        f"[127.0.0.1]:{port}",
        f"[{ALIAS}]:{port}",
    ]
    if port == 22:
        forms += ["127.0.0.1"]
    return ",".join(forms)


def _strip_managed(text: str) -> str:
    """Drop any prior onirika-managed lines, preserving everything else."""
    kept = [
        line
        for line in text.splitlines()
        if line.strip() and not line.rstrip().endswith(MARKER)
    ]
    return ("\n".join(kept) + "\n") if kept else ""


def register(host_public_line: str, port: int, path: Path | None = None) -> Path:
    """Write the gateway host key into known_hosts (idempotent).

    Args:
        host_public_line: 'ssh-ed25519 AAAA...' (keytype + base64, no host, no comment).
        port: the loopback port the gateway listens on.

    Returns the known_hosts path written.
    """
    path = path or _default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    existing = path.read_text() if path.exists() else ""
    cleaned = _strip_managed(existing)
    line = f"{_host_patterns(port)} {host_public_line} {MARKER}"

    body = cleaned.rstrip("\n")
    new_text = (body + "\n" if body else "") + line + "\n"
    path.write_text(new_text)
    os.chmod(path, 0o600)
    return path


def deregister(path: Path | None = None) -> bool:
    """Remove onirika-managed lines from known_hosts.

    Returns True if any line was removed, False otherwise.
    """
    path = path or _default_path()
    if not path.exists():
        return False
    existing = path.read_text()
    if MARKER not in existing:
        return False
    path.write_text(_strip_managed(existing))
    os.chmod(path, 0o600)
    return True


def is_registered(path: Path | None = None) -> bool:
    path = path or _default_path()
    if not path.exists():
        return False
    return MARKER in path.read_text()
