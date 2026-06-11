"""Manage the ``onirika-local-host`` block in ~/.ssh/config.

The gateway exposes a single, stable SSH alias — ``onirika-local-host`` —
that Claude Code points at once and never changes again. Behind that door the
target host can swap freely (re-run ``onirika proxy <host>``); the alias is the
fixed nameplate, the room behind it is what moves.

The block is fenced with BEGIN/END markers so removal is surgical: we only ever
touch the text between our own markers and leave every other Host entry intact.
"""

from __future__ import annotations

import os
from pathlib import Path

ALIAS = "onirika-local-host"
BEGIN_MARKER = "# BEGIN onirika-managed (onirika-local-host)"
END_MARKER = "# END onirika-managed (onirika-local-host)"


def _default_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def _render_block(user: str | None, key_path: Path, port: int) -> str:
    lines = [
        BEGIN_MARKER,
        f"Host {ALIAS}",
        "    HostName 127.0.0.1",
        f"    Port {port}",
    ]
    if user:
        lines.append(f"    User {user}")
    lines += [
        f"    IdentityFile {key_path}",
        "    IdentitiesOnly yes",
        "    StrictHostKeyChecking no",
        "    UserKnownHostsFile /dev/null",
        "    LogLevel ERROR",
        END_MARKER,
    ]
    return "\n".join(lines)


def _strip_existing(text: str) -> str:
    """Remove a previously-injected onirika block from config text, if present.

    Splits on the BEGIN/END markers and drops the fenced region plus any blank
    padding it introduced, preserving everything else byte-for-byte.
    """
    if BEGIN_MARKER not in text:
        return text

    before, _, rest = text.partition(BEGIN_MARKER)
    _, _, after = rest.partition(END_MARKER)

    # Trim a trailing newline left dangling before the block and a leading
    # newline left after it, so repeated inject/remove cycles don't accumulate
    # blank lines.
    before = before.rstrip("\n")
    after = after.lstrip("\n")

    if before and after:
        return before + "\n\n" + after
    return (before or after).rstrip("\n") + ("\n" if (before or after) else "")


def inject(
    user: str | None,
    key_path: Path,
    port: int = 4242,
    config_path: Path | None = None,
) -> Path:
    """Write (or replace) the onirika-local-host block in ~/.ssh/config.

    Idempotent: any prior onirika block is stripped first, so calling this
    twice yields the same result. Creates ~/.ssh (0o700) and the config file
    (0o600) if they do not exist.

    Returns the path to the config file that was written.
    """
    config_path = config_path or _default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(config_path.parent, 0o700)

    existing = config_path.read_text() if config_path.exists() else ""
    cleaned = _strip_existing(existing)
    block = _render_block(user, key_path, port)

    if cleaned.strip():
        new_text = cleaned.rstrip("\n") + "\n\n" + block + "\n"
    else:
        new_text = block + "\n"

    config_path.write_text(new_text)
    os.chmod(config_path, 0o600)
    return config_path


def remove(config_path: Path | None = None) -> bool:
    """Strip the onirika-local-host block from the config.

    Returns True if a block was present and removed, False if there was
    nothing to do (no file, or no onirika block).
    """
    config_path = config_path or _default_config_path()
    if not config_path.exists():
        return False

    existing = config_path.read_text()
    if BEGIN_MARKER not in existing:
        return False

    cleaned = _strip_existing(existing)
    config_path.write_text(cleaned)
    os.chmod(config_path, 0o600)
    return True


def is_injected(config_path: Path | None = None) -> bool:
    """True if an onirika-local-host block is currently present."""
    config_path = config_path or _default_config_path()
    if not config_path.exists():
        return False
    return BEGIN_MARKER in config_path.read_text()
