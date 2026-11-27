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
  onirika persist [<hostname>] [--interval N]          Ensure a persistent ControlMaster with keep-alive
  onirika clean-up [--force]                           Remove stale sockets (--force: close onirika-ctrl-*)
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


def _exec_persist(rest: list[str]) -> None:
    """Reuse an existing ControlMaster or open a persistent one with keep-alive."""
    from pathlib import Path

    from onirika.control_master import (
        DEFAULT_SERVER_ALIVE_COUNT_MAX,
        DEFAULT_SERVER_ALIVE_INTERVAL,
        persist_master,
    )

    hostname: str | None = None
    config_path: Path | None = None
    control_path: str | None = None
    port: int | None = None
    interval = DEFAULT_SERVER_ALIVE_INTERVAL
    count_max = DEFAULT_SERVER_ALIVE_COUNT_MAX
    quiet = False

    def _parse_int(flag: str, raw: str) -> int:
        try:
            val = int(raw)
        except ValueError:
            print(f"onirika persist: {flag} requires an integer, got {raw!r}", file=sys.stderr)
            sys.exit(2)
        if val <= 0:
            print(f"onirika persist: {flag} must be a positive integer", file=sys.stderr)
            sys.exit(2)
        return val

    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--config", "-c"):
            if i + 1 >= len(rest):
                print("onirika persist: --config requires a path", file=sys.stderr)
                sys.exit(2)
            config_path = Path(rest[i + 1]).expanduser()
            i += 2
        elif a.startswith("--config="):
            config_path = Path(a.split("=", 1)[1]).expanduser()
            i += 1
        elif a == "--interval":
            if i + 1 >= len(rest):
                print("onirika persist: --interval requires a number", file=sys.stderr)
                sys.exit(2)
            interval = _parse_int("--interval", rest[i + 1])
            i += 2
        elif a.startswith("--interval="):
            interval = _parse_int("--interval", a.split("=", 1)[1])
            i += 1
        elif a == "--count-max":
            if i + 1 >= len(rest):
                print("onirika persist: --count-max requires a number", file=sys.stderr)
                sys.exit(2)
            count_max = _parse_int("--count-max", rest[i + 1])
            i += 2
        elif a.startswith("--count-max="):
            count_max = _parse_int("--count-max", a.split("=", 1)[1])
            i += 1
        elif a in ("--control-path", "-S"):
            if i + 1 >= len(rest):
                print("onirika persist: --control-path requires a path", file=sys.stderr)
                sys.exit(2)
            control_path = rest[i + 1]
            i += 2
        elif a.startswith("--control-path="):
            control_path = a.split("=", 1)[1]
            i += 1
        elif a in ("--port", "-p"):
            if i + 1 >= len(rest):
                print("onirika persist: --port requires a number", file=sys.stderr)
                sys.exit(2)
            port = _parse_int("--port", rest[i + 1])
            i += 2
        elif a.startswith("--port="):
            port = _parse_int("--port", a.split("=", 1)[1])
            i += 1
        elif a in ("--quiet", "-q"):
            quiet = True
            i += 1
        elif a in ("-h", "--help"):
            print(
                "Usage: onirika persist [<hostname>] [--interval N] [--count-max N]\n"
                "                       [--control-path PATH] [--port N]\n"
                "                       [--config FILE] [--quiet]\n"
                "\n"
                "Ensure a persistent SSH ControlMaster with keep-alive pulse is running.\n"
                "Prefers reusing an existing live ControlMaster; creates a new background\n"
                "master as fallback and prints the ControlMaster socket path to stdout.\n"
                "\n"
                "  <hostname>           Onirika host alias, ~/.ssh/config alias, or\n"
                "                       [user@]hostname (defaults to config default_host)\n"
                f"  --interval N         ServerAliveInterval in seconds (default: {DEFAULT_SERVER_ALIVE_INTERVAL})\n"
                f"  --count-max N        ServerAliveCountMax missed pulses (default: {DEFAULT_SERVER_ALIVE_COUNT_MAX})\n"
                "  -S, --control-path   Explicit ControlPath socket override\n"
                "  -p, --port N         SSH port override\n"
                "  -c, --config FILE    Onirika config path\n"
                "  -q, --quiet          Only print the ControlMaster socket path"
            )
            return
        elif a.startswith("-"):
            print(f"onirika persist: unknown flag {a!r}", file=sys.stderr)
            sys.exit(2)
        else:
            if hostname is not None:
                print("onirika persist: only one hostname may be given", file=sys.stderr)
                sys.exit(2)
            hostname = a
            i += 1

    try:
        result = persist_master(
            hostname,
            interval=interval,
            count_max=count_max,
            control_path=control_path,
            port=port,
            config_path=config_path,
        )
    except FileNotFoundError as e:
        print(f"onirika persist: {e}", file=sys.stderr)
        print("Run 'onirika setup' first, or pass a hostname directly.", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"onirika persist: {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"onirika persist: {e}", file=sys.stderr)
        sys.exit(1)

    if not quiet:
        if result.reused:
            pid_info = f" (pid={result.pid})" if result.pid is not None else ""
            print(
                f"Reusing existing ControlMaster for {result.display_name}{pid_info}",
                file=sys.stderr,
                flush=True,
            )
        else:
            pid_part = f"pid={result.pid}, " if result.pid is not None else ""
            print(
                f"Opened ControlMaster for {result.display_name} "
                f"({pid_part}keep-alive every {result.interval}s x {result.count_max})",
                file=sys.stderr,
                flush=True,
            )

    print(result.control_path)


def _exec_cleanup(rest: list[str]) -> None:
    """Remove stale sockets. With --force, close onirika-ctrl-* masters."""
    from pathlib import Path

    from onirika.control_master import ONIRIKA_SOCKET_PREFIX, cleanup_masters

    force = False
    config_path: Path | None = None

    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("--force", "-f"):
            force = True
            i += 1
        elif a in ("--config", "-c"):
            if i + 1 >= len(rest):
                print("onirika clean-up: --config requires a path", file=sys.stderr)
                sys.exit(2)
            config_path = Path(rest[i + 1]).expanduser()
            i += 2
        elif a.startswith("--config="):
            config_path = Path(a.split("=", 1)[1]).expanduser()
            i += 1
        elif a in ("-h", "--help"):
            print(
                "Usage: onirika clean-up [--force] [--config FILE]\n"
                "\n"
                "Remove stale Onirika ControlMaster sockets.\n"
                "\n"
                "  -f, --force         Also close live masters whose socket name\n"
                "                      starts with onirika-ctrl-\n"
                "  -c, --config FILE   Onirika config path"
            )
            return
        elif a.startswith("-"):
            print(f"onirika clean-up: unknown flag {a!r}", file=sys.stderr)
            sys.exit(2)
        else:
            print(f"onirika clean-up: unexpected argument {a!r}", file=sys.stderr)
            sys.exit(2)

    try:
        items = cleanup_masters(force=force, config_path=config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"onirika clean-up: {e}", file=sys.stderr)
        sys.exit(1)

    if not items:
        print("No Onirika control masters found.")
        return

    removed_stale = 0
    closed_active = 0
    kept_active = 0
    closable = False  # a kept socket that --force would close
    failed = 0

    for item in items:
        pid_info = f" (pid={item.pid})" if item.pid is not None else ""
        if item.status == "removed_stale":
            removed_stale += 1
            print(f"Removed stale ControlMaster: {item.control_path}")
        elif item.status == "pruned_missing":
            removed_stale += 1
            print(f"Pruned missing ControlMaster entry: {item.control_path}")
        elif item.status == "closed_active":
            closed_active += 1
            print(f"Closed active ControlMaster: {item.control_path}{pid_info}")
        elif item.status == "kept_active":
            kept_active += 1
            if Path(item.control_path).name.startswith(ONIRIKA_SOCKET_PREFIX):
                closable = True
            print(f"Active ControlMaster left running: {item.control_path}{pid_info}")
        else:
            failed += 1
            print(
                f"Failed to clean up {item.control_path}: {item.detail}",
                file=sys.stderr,
            )

    parts: list[str] = []
    if removed_stale:
        parts.append(f"{removed_stale} stale removed")
    if closed_active:
        parts.append(f"{closed_active} active closed")
    if kept_active:
        parts.append(f"{kept_active} active left running")
    if failed:
        parts.append(f"{failed} failed")
    print(f"Summary: {', '.join(parts)}.")

    if kept_active:
        if force:
            print("--force closes only sockets named onirika-ctrl-*.")
        elif closable:
            print("Use 'onirika clean-up --force' to close active onirika-ctrl-* masters.")

    if failed:
        sys.exit(1)


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

    if cmd == "persist":
        _exec_persist(rest)
        return

    if cmd in ("clean-up", "cleanup"):
        _exec_cleanup(rest)
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
