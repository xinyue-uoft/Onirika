"""Top-level `onirika` command dispatcher.

Routes subcommands to existing entry points without subprocess-ing through
`uv run`, so cold start is fast enough for the MCP handshake.
"""

from __future__ import annotations

import os
import sys
from importlib.resources import as_file, files

USAGE = """\
onirika — MCP SSH dev launcher

Usage:
  onirika setup                       Interactive setup wizard
  onirika ssh [--config FILE]         Run the MCP SSH server (stdio)
  onirika launch <host> [--clean]     tmux + SSH master + Claude Code (Unix)
  onirika web [host] [--port N]       Web UI
  onirika help                        Show this message
"""


def _exec_launcher(rest: list[str]) -> None:
    """Locate the bundled launcher shell script and exec it under bash.

    Sets ONIRIKA_PYTHON so the bash side uses the same interpreter that owns
    the pyyaml dependency — avoids relying on system python3 having yaml.
    """
    resource = files("onirika") / "scripts" / "onirika-launch.sh"
    with as_file(resource) as path:
        env = {**os.environ, "ONIRIKA_PYTHON": sys.executable}
        os.execvpe("bash", ["bash", str(path), *rest], env)


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        print(USAGE)
        return

    cmd, *rest = argv

    if cmd in ("help", "-h", "--help"):
        print(USAGE)
        return

    if cmd == "setup":
        sys.argv = ["onirika-setup", *rest]
        from onirika.setup import main as setup_main
        setup_main()
        return

    if cmd == "ssh":
        sys.argv = ["onirika-ssh", *rest]
        from onirika.__main__ import main as ssh_main
        ssh_main()
        return

    if cmd == "launch":
        _exec_launcher(rest)
        return

    if cmd == "web":
        sys.argv = ["onirika-web", *rest]
        from onirika.web.__main__ import main as web_main
        web_main()
        return

    print(f"onirika: unknown command {cmd!r}", file=sys.stderr)
    print("Run 'onirika help' for usage.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
