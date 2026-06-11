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
  onirika proxy [<host>] [--establish] [--source PATH] Local SSH gateway on 127.0.0.1:4242 for Claude Code
  onirika proxy status                                 Show whether the gateway is running
  onirika proxy stop                                   Tear down the ~/.ssh/config gateway alias
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


def _exec_proxy(rest: list[str]) -> None:
    """Dispatch `onirika proxy` and its `status` / `stop` subcommands.

    The gateway is a purely additive consumer of an existing ControlMaster
    socket — it never touches authentication, and never stops the master (that
    belongs to whoever ran `onirika establish`). On exit it only removes its own
    ~/.ssh/config alias.
    """
    if rest and rest[0] == "status":
        _proxy_status()
        return
    if rest and rest[0] == "stop":
        _proxy_stop()
        return

    _proxy_run(rest)


def _proxy_status() -> None:
    """Report whether the gateway alias is present and the port is listening."""
    import socket as _socket

    from onirika import known_hosts, ssh_config
    from onirika.proxy import DEFAULT_PORT

    injected = ssh_config.is_injected()
    registered = known_hosts.is_registered()
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        listening = s.connect_ex(("127.0.0.1", DEFAULT_PORT)) == 0

    alias = ssh_config.ALIAS
    print(f"ssh config alias '{alias}': {'present' if injected else 'absent'}")
    print(f"known_hosts entry: {'present' if registered else 'absent'}")
    print(f"gateway 127.0.0.1:{DEFAULT_PORT}: {'listening' if listening else 'not listening'}")
    if (injected or registered) and not listening:
        print("(stale state — no gateway is running. 'onirika proxy stop' to clean it up.)")


def _proxy_stop() -> None:
    """Remove the gateway alias from ~/.ssh/config (does not touch the master)."""
    from onirika import known_hosts, ssh_config

    removed = ssh_config.remove()
    kh_removed = known_hosts.deregister()
    if removed or kh_removed:
        print(f"Removed '{ssh_config.ALIAS}' from ~/.ssh/config and known_hosts.")
    else:
        print(f"No '{ssh_config.ALIAS}' alias present; nothing to remove.")


def _proxy_run(rest: list[str]) -> None:
    """Parse args, optionally establish the master, then run the gateway."""
    import subprocess
    from pathlib import Path

    from onirika.config import load_config
    from onirika.proxy import DEFAULT_PORT

    alias: str | None = None
    config_path: Path | None = None
    port = DEFAULT_PORT
    do_establish = False
    sources: list[str] = []

    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--config", "-c"):
            if i + 1 >= len(rest):
                print("onirika proxy: --config requires a path", file=sys.stderr)
                sys.exit(2)
            config_path = Path(rest[i + 1]).expanduser()
            i += 2
        elif a.startswith("--config="):
            config_path = Path(a.split("=", 1)[1]).expanduser()
            i += 1
        elif a in ("--port", "-p"):
            if i + 1 >= len(rest):
                print("onirika proxy: --port requires a number", file=sys.stderr)
                sys.exit(2)
            port = int(rest[i + 1])
            i += 2
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
            i += 1
        elif a == "--establish":
            do_establish = True
            i += 1
        elif a == "--source":
            if i + 1 >= len(rest):
                print("onirika proxy: --source requires a path", file=sys.stderr)
                sys.exit(2)
            sources.append(rest[i + 1])
            i += 2
        elif a.startswith("--source="):
            sources.append(a.split("=", 1)[1])
            i += 1
        elif a in ("-h", "--help"):
            print(
                "Usage: onirika proxy [<host>] [--establish] [--port N]\n"
                "                     [--source PATH ...] [--config FILE]\n"
                "       onirika proxy status\n"
                "       onirika proxy stop\n"
                "\n"
                "  --source PATH   shell script to `source` before every command the\n"
                "                  gateway forwards (e.g. a proxy script). Scoped to\n"
                "                  the agent's processes; your login shell is untouched.\n"
                "                  Repeatable. loopback (127.0.0.1/localhost/::1) is\n"
                "                  auto-added to NO_PROXY so it never routes via a proxy."
            )
            return
        elif a.startswith("-"):
            print(f"onirika proxy: unknown flag {a!r}", file=sys.stderr)
            sys.exit(2)
        else:
            if alias is not None:
                print("onirika proxy: only one host alias may be given", file=sys.stderr)
                sys.exit(2)
            alias = a
            i += 1

    try:
        cfg = load_config(config_path)
    except FileNotFoundError as e:
        print(f"onirika proxy: {e}", file=sys.stderr)
        print("Run 'onirika setup' first.", file=sys.stderr)
        sys.exit(1)

    if alias is None:
        alias = cfg.default_host
        if not alias:
            print(
                "onirika proxy: no host given and no default_host in config.",
                file=sys.stderr,
            )
            sys.exit(2)

    try:
        host = cfg.get_host(alias)
    except ValueError as e:
        print(f"onirika proxy: {e}", file=sys.stderr)
        sys.exit(1)

    control_path = host.resolved_control_path

    if do_establish:
        # Open the master in the background after interactive auth (Kerberos/2FA
        # happens right here in the user's terminal). -f backgrounds ssh once
        # authenticated; -N runs no remote command.
        argv = ["ssh", "-M", "-S", control_path, "-f", "-N"]
        if host.port != 22:
            argv += ["-p", str(host.port)]
        argv.append(host.ssh_target)
        print(f"Establishing ControlMaster for {alias} ({host.ssh_target})...")
        result = subprocess.run(argv)
        if result.returncode != 0:
            print(
                f"onirika proxy: failed to establish master (ssh exit {result.returncode}).",
                file=sys.stderr,
            )
            sys.exit(1)

    preamble = _compose_preamble(sources)
    _run_gateway(host, alias, port, preamble)


def _compose_preamble(sources: list[str]) -> str:
    """Build the per-command shell prefix from --source scripts.

    Each script is sourced (output muted so it can't corrupt the command's
    stream), then loopback is appended to NO_PROXY/no_proxy as a safety net —
    127.0.0.1/localhost/::1 must never be routed through an HTTP proxy. Paths
    are left unquoted so `~user` tilde expansion works on the remote.
    """
    if not sources:
        return ""
    parts = [f"source {s} >/dev/null 2>&1" for s in sources]
    parts.append('export NO_PROXY="${NO_PROXY:+$NO_PROXY,}localhost,127.0.0.1,::1"')
    parts.append('export no_proxy="${no_proxy:+$no_proxy,}localhost,127.0.0.1,::1"')
    return "; ".join(parts)


def _run_gateway(host, alias: str, port: int, preamble: str = "") -> None:
    """Verify the master socket, inject the ssh alias, and serve until Ctrl+C."""
    import asyncio

    import asyncssh

    from onirika import known_hosts, proxy_key, ssh_config
    from onirika.proxy import start_proxy
    from onirika.ssh import SSHExecutor

    async def _main() -> None:
        executor = SSHExecutor(host)
        ok, message = await executor.check_connection(force=True)
        if not ok:
            print(f"onirika proxy: {message}", file=sys.stderr)
            print(
                f"\nNo live ControlMaster for {alias}. Either run\n"
                f"  onirika establish {alias}\n"
                f"in another terminal first, or re-run with --establish:\n"
                f"  onirika proxy {alias} --establish",
                file=sys.stderr,
            )
            sys.exit(1)

        priv, pub = proxy_key.ensure_keypair()
        ssh_config.inject(host.user, priv, port=port)

        # Persistent host key + known_hosts registration: Claude Code verifies
        # the host key against ~/.ssh/known_hosts with no trust-on-first-use,
        # so the key must be stable and pre-registered.
        host_key_path = proxy_key.ensure_host_key()
        host_key = asyncssh.read_private_key(str(host_key_path))
        known_hosts.register(proxy_key.host_public_line(host_key_path), port)

        acceptor = await start_proxy(
            host, pub, port=port, host_key=host_key, preamble=preamble
        )

        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        import signal as _signal
        for sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover — non-Unix
                pass

        print(f"onirika-local-host  →  {alias} ({host.ssh_target})")
        print(f"listening on 127.0.0.1:{port}")
        if preamble:
            print(f"env preamble (forwarded commands only): {preamble}")
        print(f"Claude Code: connect to SSH host '{ssh_config.ALIAS}'.")
        print("Press Ctrl+C to tear down the gateway (the ControlMaster stays up).")

        try:
            await stop.wait()
        finally:
            print("\nTearing down gateway...")
            acceptor.close()
            await acceptor.wait_closed()
            ssh_config.remove()
            known_hosts.deregister()
            print(
                f"Removed '{ssh_config.ALIAS}' from ~/.ssh/config and known_hosts. "
                f"ControlMaster left untouched."
            )

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


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

    if cmd == "proxy":
        _exec_proxy(rest)
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
