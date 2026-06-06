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
  onirika setup                                        Interactive setup wizard
  onirika ssh [--config FILE]                          Run the MCP SSH server (stdio)
  onirika establish [<host>] [--config FILE]           Open the SSH master for <host> (or default_host)
  onirika launch [--agent claude|opencode] <host>      tmux + SSH master + agent (Unix)
  onirika web [host] [--port N]                        Web UI
  onirika help                                         Show this message

Environment:
  ONIRIKA_AGENT    Default agent for `launch` when --agent is not given.
  ONIRIKA_CONFIG   Config path (defaults to ~/.config/onirika/config.yaml).
"""


def _exec_establish(rest: list[str]) -> None:
    """Open the SSH ControlMaster connection for a configured host.

    Equivalent to `ssh -M -S <control_path> [-p PORT] <user>@<ssh_host>`,
    with `~` expanded and the SSH %r/%h/%p tokens left for ssh to handle.
    """
    from pathlib import Path

    from onirika.config import load_config

    alias: str | None = None
    config_path: Path | None = None

    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--config", "-c"):
            if i + 1 >= len(rest):
                print("onirika establish: --config requires a path", file=sys.stderr)
                sys.exit(2)
            config_path = Path(rest[i + 1]).expanduser()
            i += 2
        elif a.startswith("--config="):
            config_path = Path(a.split("=", 1)[1]).expanduser()
            i += 1
        elif a in ("-h", "--help"):
            print("Usage: onirika establish [<host>] [--config FILE]")
            return
        elif a.startswith("-"):
            print(f"onirika establish: unknown flag {a!r}", file=sys.stderr)
            sys.exit(2)
        else:
            if alias is not None:
                print("onirika establish: only one host alias may be given", file=sys.stderr)
                sys.exit(2)
            alias = a
            i += 1

    try:
        cfg = load_config(config_path)
    except FileNotFoundError as e:
        print(f"onirika establish: {e}", file=sys.stderr)
        print("Run 'onirika setup' first.", file=sys.stderr)
        sys.exit(1)

    if alias is None:
        alias = cfg.default_host
        if not alias:
            print(
                "onirika establish: no host given and no default_host in config.",
                file=sys.stderr,
            )
            sys.exit(2)

    try:
        host = cfg.get_host(alias)
    except ValueError as e:
        print(f"onirika establish: {e}", file=sys.stderr)
        sys.exit(1)

    control_path = os.path.expanduser(host.control_path)
    argv = ["ssh", "-M", "-S", control_path]
    if host.port != 22:
        argv += ["-p", str(host.port)]
    argv.append(host.ssh_target)

    os.execvp("ssh", argv)


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

    if cmd == "establish":
        _exec_establish(rest)
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
