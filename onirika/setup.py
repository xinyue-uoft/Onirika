"""Interactive setup wizard for Onirika SSH MCP Server."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from onirika.config import HostConfig, load_config
from onirika.ssh import SSHExecutor

console = Console()

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path.home() / ".config" / "onirika"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
SSH_CONFIG_PATH = Path.home() / ".ssh" / "config"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _check_binary(name: str) -> str | None:
    """Return path to binary or None."""
    return shutil.which(name)


def _run_quiet(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run a command and return (returncode, combined output)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return -1, str(e)


def _step(n: int, title: str):
    console.print(f"\n[bold cyan]Step {n}[/] — [bold]{title}[/]")
    console.print("─" * 50)


def _parse_ssh_config() -> list[dict]:
    """Parse ~/.ssh/config and return a list of Host block dicts.

    Each dict has:
      host_pattern: str  — the Host line value (may contain wildcards)
      hostname: str | None
      user: str | None
      port: int | None
      proxyjump: str | None
      identityfile: str | None
      raw_options: dict  — all other key-value pairs
    """
    if not SSH_CONFIG_PATH.exists():
        return []

    hosts: list[dict] = []
    current: dict | None = None

    # Keys we extract explicitly
    EXTRACT_KEYS = {"hostname", "user", "port", "proxyjump", "identityfile"}

    with open(SSH_CONFIG_PATH) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            # Split on first whitespace or =
            if "=" in line and " " not in line.split("=")[0]:
                key, _, value = line.partition("=")
            else:
                key, _, value = line.partition(" ")
                if not value:
                    key, _, value = line.partition("\t")

            key = key.strip()
            value = value.strip()

            if key.lower() == "host":
                # Start a new block
                current = {
                    "host_pattern": value,
                    "hostname": None,
                    "user": None,
                    "port": None,
                    "proxyjump": None,
                    "identityfile": None,
                    "raw_options": {},
                }
                hosts.append(current)
            elif current is not None:
                lower_key = key.lower()
                if lower_key in EXTRACT_KEYS:
                    if lower_key == "port":
                        try:
                            current["port"] = int(value)
                        except ValueError:
                            current["port"] = None
                    else:
                        current[lower_key] = value
                else:
                    current["raw_options"][key] = value

    return hosts


def _display_ssh_hosts(hosts: list[dict]) -> None:
    """Print a numbered table of parsed SSH hosts."""
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("#", style="dim", width=4)
    table.add_column("Host pattern")
    table.add_column("HostName", style="cyan")
    table.add_column("User", style="dim")
    table.add_column("Via", style="dim")

    for i, h in enumerate(hosts, 1):
        via = h.get("proxyjump") or ""
        table.add_row(
            str(i),
            h["host_pattern"],
            h.get("hostname") or "[dim]—[/]",
            h.get("user") or "[dim]—[/]",
            via if via else "[dim]—[/]",
        )

    console.print(table)


# ── Steps ────────────────────────────────────────────────────────────────────

def step_welcome():
    console.print(Panel.fit(
        "[bold]Onirika SSH Setup Wizard[/]\n\n"
        "This will configure Onirika so Claude Code can\n"
        "read files, run commands, and manage code on\n"
        "your remote SSH server.\n\n"
        "[dim]You will need:[/]\n"
        "  • The hostname and username for your server\n"
        "  • SSH access (key, Kerberos, or password)\n"
        "  • A terminal to test the connection",
        border_style="cyan",
    ))


def step_prereqs() -> bool:
    _step(1, "Checking prerequisites")

    # Required
    ssh_path = _check_binary("ssh")
    if ssh_path:
        console.print(f"  [green]✓[/] OpenSSH client — {ssh_path}")
    else:
        console.print(f"  [red]✗[/] OpenSSH client — [red]not found (required)[/]")
        console.print("\n[red]ssh is required. Please install OpenSSH first.[/]")
        return False

    # Optional but useful
    tmux_path = _check_binary("tmux")
    if tmux_path:
        console.print(f"  [green]✓[/] tmux — {tmux_path}")
    else:
        console.print(f"  [yellow]![/] tmux — not found [dim](optional, needed for onirika-launch)[/]")

    claude_path = _check_binary("claude")
    if claude_path:
        console.print(f"  [green]✓[/] Claude Code CLI — {claude_path}")
    else:
        console.print(f"  [yellow]![/] Claude Code CLI — not found [dim](needed for MCP registration)[/]")

    uv_path = _check_binary("uv")
    if uv_path:
        console.print(f"  [green]✓[/] uv package manager — {uv_path}")
    else:
        console.print(f"  [yellow]![/] uv — not found [dim](will use python directly)[/]")

    return True


def step_configure_host() -> dict:
    _step(2, "Configure remote host")

    imported = _try_import_from_ssh_config()
    if imported:
        return imported

    return _configure_host_manual()


def _try_import_from_ssh_config() -> dict | None:
    """Offer to import a host from ~/.ssh/config. Returns None if user declines."""
    ssh_hosts = _parse_ssh_config()
    # Filter out trivial entries (like "Host *")
    ssh_hosts = [h for h in ssh_hosts if h["host_pattern"] != "*"]

    if not ssh_hosts:
        console.print("  [dim]No hosts found in ~/.ssh/config.[/]")
        return None

    use_ssh = Confirm.ask(
        f"  Import from ~/.ssh/config? [dim]({len(ssh_hosts)} host(s) found)[/]",
        default=True,
    )
    if not use_ssh:
        return None

    console.print()
    _display_ssh_hosts(ssh_hosts)
    console.print()

    choice = Prompt.ask(
        "  Enter host number to import [dim](or 0 to skip)[/]",
        default="1",
    )
    try:
        idx = int(choice) - 1
    except ValueError:
        idx = -1

    if idx < 0 or idx >= len(ssh_hosts):
        console.print("  [dim]Skipping import, entering details manually.[/]")
        return None

    selected = ssh_hosts[idx]
    pattern = selected["host_pattern"]
    hostname = selected.get("hostname") or ""
    user = selected.get("user") or ""
    port = selected.get("port") or 22
    proxyjump = selected.get("proxyjump") or ""

    console.print()
    console.print(f"  Selected: [bold]{pattern}[/]")
    if proxyjump:
        console.print(f"  [dim]Via proxy: {proxyjump}[/]")

    # Handle wildcard patterns — need to ask for actual hostname
    is_wildcard = any(c in pattern for c in "*?[")
    if is_wildcard:
        console.print()
        console.print(f"  [yellow]![/] This is a wildcard pattern: [bold]{pattern}[/]")
        console.print("  [dim]Which specific host do you connect to?[/]")
        actual_host = Prompt.ask(
            "  Actual hostname",
            default=hostname if hostname and not any(c in hostname for c in "*?[") else "",
        )
        if not actual_host:
            console.print("  [red]Hostname is required.[/]")
            return None
        # The SSH alias is the pattern-matched name the user types
        # e.g., if pattern is "dev-*" and they connect to "dev-003"
        ssh_alias = Prompt.ask(
            "  SSH alias [dim](what you type after 'ssh')[/]",
            default=actual_host,
        )
        ssh_host = actual_host
    else:
        ssh_alias = pattern
        ssh_host = hostname or pattern

    # Onirika alias (short name for config)
    alias = Prompt.ask(
        "  Onirika alias [dim](short name for this host in config)[/]",
        default=ssh_alias.split(".")[0],  # use first component as default
    )

    console.print()
    console.print("  [bold]Imported settings:[/]")
    console.print(f"    SSH host:  {ssh_host}")
    if user:
        console.print(f"    User:      {user}")
    console.print(f"    Port:      {port}")
    if proxyjump:
        console.print(f"    ProxyJump: {proxyjump}")

    # Now ask for Onirika-specific settings not in SSH config
    console.print()
    default_cwd = Prompt.ask(
        "  Default working directory on remote",
        default="~",
    )

    preamble = _ask_preamble()

    # For ControlMaster: use the SSH alias as the target, so SSH config
    # handles all routing (ProxyJump, HostName, etc.)
    # The control_path uses the alias, not the raw hostname
    control_path = "~/.ssh/ctrl-%r@%h:%p"

    host_config = {
        "ssh_host": ssh_alias,  # Use the SSH alias so ssh config handles routing
        "port": port,
        "control_path": control_path,
        "default_cwd": default_cwd,
        "command_timeout": 30,
        "file_timeout": 60,
    }
    if user:
        host_config["user"] = user
    if preamble:
        host_config["preamble"] = preamble

    # Flag that SSH config already has this host defined
    host_config["_ssh_config_managed"] = True

    return {"alias": alias, "host_config": host_config}


def _ask_preamble() -> str:
    """Ask user about environment setup scripts."""
    has_preamble = Confirm.ask(
        "  Do you source setup scripts before working? [dim](e.g. source ~/env.sh)[/]",
        default=False,
    )
    if not has_preamble:
        return ""

    console.print("  [dim]Enter source commands, one per line. Empty line to finish:[/]")
    preamble_lines = []
    while True:
        line = Prompt.ask("  ", default="")
        if not line:
            break
        preamble_lines.append(line)
    return "\n".join(preamble_lines)


def _configure_host_manual() -> dict:
    """Manual host configuration (no SSH config import)."""
    alias = Prompt.ask(
        "  Host alias [dim](short name for this server)[/]",
        default="myserver",
    )
    ssh_host = Prompt.ask("  SSH hostname [dim](e.g. server.example.com)[/]")
    user = Prompt.ask(
        "  SSH username [dim](leave blank to use system default)[/]",
        default="",
    )
    port_str = Prompt.ask("  SSH port", default="22")
    port = int(port_str) if port_str.isdigit() else 22

    console.print()
    default_cwd = Prompt.ask(
        "  Default working directory on remote",
        default="~",
    )

    console.print()
    preamble = _ask_preamble()

    control_path = "~/.ssh/ctrl-%r@%h:%p"

    host_config = {
        "ssh_host": ssh_host,
        "port": port,
        "control_path": control_path,
        "default_cwd": default_cwd,
        "command_timeout": 30,
        "file_timeout": 60,
    }
    if user:
        host_config["user"] = user
    if preamble:
        host_config["preamble"] = preamble

    return {"alias": alias, "host_config": host_config}


def step_write_config(alias: str, host_config: dict) -> bool:
    _step(3, "Write configuration")

    # Strip internal flags before writing
    write_config = {k: v for k, v in host_config.items() if not k.startswith("_")}

    config_data = {
        "default_host": alias,
        "hosts": {alias: write_config},
    }

    if CONFIG_PATH.exists():
        console.print(f"  Config already exists: [bold]{CONFIG_PATH}[/]")
        if Confirm.ask("  Overwrite existing config?", default=False):
            pass
        elif Confirm.ask("  Add this host to existing config?", default=True):
            with open(CONFIG_PATH) as f:
                existing = yaml.safe_load(f) or {}
            existing.setdefault("hosts", {})[alias] = write_config
            if not existing.get("default_host"):
                existing["default_host"] = alias
            config_data = existing
        else:
            console.print("  [yellow]Skipped.[/] Using existing config.")
            return True

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

    console.print(f"  [green]✓[/] Config written to [bold]{CONFIG_PATH}[/]")
    return True


def step_ssh_config(alias: str, host_config: dict) -> bool:
    _step(4, "SSH ControlMaster configuration")

    ssh_host = host_config["ssh_host"]
    user = host_config.get("user", "")
    port = host_config.get("port", 22)
    control_path = host_config.get("control_path", "~/.ssh/ctrl-%r@%h:%p")
    from_ssh_config = host_config.get("_ssh_config_managed", False)

    # If imported from SSH config, just offer to add ControlMaster options
    if from_ssh_config:
        console.print(f"  Host was imported from [bold]~/.ssh/config[/].")
        console.print("  Checking if ControlMaster is already configured...\n")

        # Check if the existing SSH config block already has ControlMaster
        if SSH_CONFIG_PATH.exists():
            content = SSH_CONFIG_PATH.read_text()
            if "ControlMaster" in content:
                console.print("  [green]✓[/] ControlMaster is already configured in ~/.ssh/config.")
                console.print("  [dim]Make sure it applies to this host (check Host patterns).[/]")
                return True

        console.print("  [yellow]![/] No ControlMaster found in ~/.ssh/config.")
        console.print("  Onirika needs ControlMaster to piggyback on your SSH session.\n")
        console.print("  You can either:")
        console.print(f"    1. Add ControlMaster to the existing host block")
        console.print(f"    2. Add a global [bold]Host *[/] block with ControlMaster")
        console.print()

        if Confirm.ask("  Add a global ControlMaster block to ~/.ssh/config?", default=True):
            global_block = f"""
Host *
    ControlMaster auto
    ControlPath {control_path}
    ControlPersist 10m
    ServerAliveInterval 60
    ServerAliveCountMax 3"""

            console.print(Panel(global_block.strip(), title="Global SSH config", border_style="dim"))
            if Confirm.ask("  Append this?", default=True):
                SSH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(SSH_CONFIG_PATH, "a") as f:
                    f.write("\n" + global_block.strip() + "\n")
                os.chmod(SSH_CONFIG_PATH, 0o600)
                console.print(f"  [green]✓[/] Added to {SSH_CONFIG_PATH}")
            else:
                console.print("  [dim]Skipped.[/]")
        else:
            console.print("  [dim]You'll need to add ControlMaster manually or use:[/]")
            target = f"{user}@{ssh_host}" if user else ssh_host
            console.print(f"  [cyan]ssh -M -S {control_path} {target}[/]")

        return True

    # Manual host — offer a full SSH config block
    block = f"""
Host {alias}
    HostName {ssh_host}
    ControlMaster auto
    ControlPath {control_path}
    ControlPersist 10m
    ServerAliveInterval 60
    ServerAliveCountMax 3"""

    if user:
        block += f"\n    User {user}"
    if port != 22:
        block += f"\n    Port {port}"

    console.print("  Adding this to [bold]~/.ssh/config[/] lets you connect with just")
    console.print(f"  [cyan]ssh {alias}[/] and enables automatic ControlMaster sharing.\n")
    console.print(Panel(block.strip(), title="SSH config block", border_style="dim"))

    # Check if this host is already configured
    if SSH_CONFIG_PATH.exists():
        existing = SSH_CONFIG_PATH.read_text()
        if f"Host {alias}" in existing:
            console.print(f"  [yellow]![/] Host '{alias}' already exists in ~/.ssh/config. Skipping.")
            return True

    if not Confirm.ask("  Add this block to ~/.ssh/config?", default=True):
        console.print("  [dim]Skipped. You can add it manually later.[/]")
        console.print(f"  Without it, use the full command:")
        target = f"{user}@{ssh_host}" if user else ssh_host
        console.print(f"  [cyan]ssh -M -S {control_path} {target}[/]")
        return True

    SSH_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SSH_CONFIG_PATH, "a") as f:
        f.write("\n" + block.strip() + "\n")

    # Ensure correct permissions
    os.chmod(SSH_CONFIG_PATH, 0o600)
    console.print(f"  [green]✓[/] Added to {SSH_CONFIG_PATH}")
    return True


def _resolve_mcp_command() -> tuple[list[str], str]:
    """Pick the fastest-starting invocation for the MCP server.

    Returns (argv, label). The first element of argv is an absolute path so
    Claude Code does not depend on its own PATH at spawn time. We avoid
    `uv run` because dependency resolution on cold start can exceed Claude
    Code's MCP handshake window, manifesting as "Failed to connect" even
    though the server itself is healthy.
    """
    # 1. Globally installed entry point (uv tool install / pipx).
    on_path = _check_binary("onirika-ssh")
    venv_script = PROJECT_DIR / ".venv" / "bin" / "onirika-ssh"
    if on_path and Path(on_path).resolve() != venv_script.resolve():
        return [on_path], f"global install ({on_path})"

    # 2. Project venv entry point — fine for dev, still skips uv resolution.
    if venv_script.exists():
        return [str(venv_script)], f"dev venv ({venv_script})"

    # 3. Last resort — uv run. Slowest cold start; may time out.
    if _check_binary("uv"):
        return (
            ["uv", "--directory", str(PROJECT_DIR), "run", "onirika-ssh"],
            "uv run (slow cold start — install with `uv tool install .` for best results)",
        )

    raise RuntimeError(
        "Cannot locate onirika-ssh. Run `uv tool install .` from the repo "
        "or `uv sync` to create a project venv."
    )


def step_register_mcp() -> bool:
    _step(5, "Register MCP server with Claude Code")

    claude_path = _check_binary("claude")
    try:
        mcp_cmd, label = _resolve_mcp_command()
    except RuntimeError as e:
        console.print(f"  [red]✗[/] {e}")
        return True

    if not claude_path:
        console.print("  [yellow]![/] Claude Code CLI not found.")
        console.print("  You can register manually later with:")
        console.print(
            f"  [cyan]claude mcp add --scope user onirika-ssh -- {' '.join(mcp_cmd)}[/]"
        )
        return True

    console.print(f"  [dim]Will register: {label}[/]")

    rc, output = _run_quiet(["claude", "mcp", "list"])
    if rc == 0 and "onirika-ssh" in output:
        console.print("  [green]✓[/] onirika-ssh is already registered with Claude Code.")
        if Confirm.ask("  Re-register (overwrite)?", default=False):
            _run_quiet(["claude", "mcp", "remove", "onirika-ssh"])
        else:
            return True

    if not Confirm.ask("  Register onirika-ssh as a Claude Code MCP server?", default=True):
        console.print("  [dim]Skipped.[/]")
        return True

    cmd = [
        "claude", "mcp", "add",
        "--transport", "stdio",
        "--scope", "user",
        "onirika-ssh",
        "--",
        *mcp_cmd,
    ]

    rc, output = _run_quiet(cmd, timeout=30)
    if rc == 0:
        console.print("  [green]✓[/] MCP server registered with Claude Code.")
    else:
        console.print(f"  [red]✗[/] Registration failed: {output}")
        console.print("  You can try manually:")
        console.print(f"  [cyan]{' '.join(cmd)}[/]")

    return True


def step_test_connection(alias: str) -> bool:
    _step(6, "Test SSH connection")

    try:
        config = load_config(CONFIG_PATH)
        host_config = config.get_host(alias)
    except Exception as e:
        console.print(f"  [red]✗[/] Could not load config: {e}")
        return False

    target = host_config.ssh_target
    control_path = host_config.control_path

    # Check if already connected
    executor = SSHExecutor(host_config)
    connected, _ = asyncio.run(_check(executor))

    if connected:
        console.print(f"  [green]✓[/] Already connected to {target}!")
    else:
        console.print(f"  No active connection to {target}.")
        console.print()
        console.print("  Please open [bold]another terminal[/] and run:")
        console.print()

        has_ssh_config = False
        if SSH_CONFIG_PATH.exists():
            has_ssh_config = f"Host {alias}" in SSH_CONFIG_PATH.read_text()

        if has_ssh_config:
            console.print(f"    [bold cyan]ssh {alias}[/]")
        else:
            console.print(f"    [bold cyan]ssh -M -S {control_path} {target}[/]")

        console.print()
        console.print("  [dim]Complete any kinit / 2FA prompts in that terminal,[/]")
        console.print("  [dim]then come back here and press Enter.[/]")

        Prompt.ask("\n  Press [bold]Enter[/] when connected", default="")

        # Re-check
        connected, msg = asyncio.run(_check(executor))
        if connected:
            console.print(f"  [green]✓[/] Connection verified!")
        else:
            console.print(f"  [yellow]![/] Still not connected: {msg}")
            if Confirm.ask("  Continue anyway?", default=True):
                return True
            return False

    # Run quick smoke test
    if connected and Confirm.ask("  Run a quick smoke test?", default=True):
        return asyncio.run(_smoke_test(executor))

    return True


async def _check(executor: SSHExecutor) -> tuple[bool, str]:
    return await executor.check_connection(force=True)


async def _smoke_test(executor: SSHExecutor) -> bool:
    tests = [
        ("echo hello", "Run echo command"),
        ("pwd", "Check working directory"),
        ("ls | head -5", "List files"),
        ("git --version 2>/dev/null || echo 'git not available'", "Check git"),
    ]
    all_ok = True
    for cmd, desc in tests:
        result = await executor.run(cmd, timeout=10)
        if result.exit_code == 0:
            output = result.stdout.strip().split("\n")[0][:60]
            console.print(f"  [green]✓[/] {desc}: [dim]{output}[/]")
        else:
            console.print(f"  [red]✗[/] {desc}: {result.stderr.strip()[:60]}")
            all_ok = False

    if all_ok:
        console.print("  [green]All checks passed![/]")
    return all_ok


def step_done(alias: str):
    _step(7, "Setup complete!")

    has_tmux = _check_binary("tmux") is not None

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    if has_tmux:
        table.add_row("Launch:", f"[cyan]onirika launch {alias}[/]")
        table.add_row("", "[dim]Opens tmux with SSH + Claude Code side by side[/]")
        table.add_row("", "")
        table.add_row("Or manually:", "")
    else:
        table.add_row("Launch:", "[dim](install tmux for one-command launch)[/]")
        table.add_row("", "")

    table.add_row("  Terminal 1:", f"[cyan]ssh {alias}[/]  [dim](authenticate here)[/]")
    table.add_row("  Terminal 2:", "[cyan]claude[/]  [dim](AI works on remote files)[/]")
    table.add_row("", "")
    table.add_row("Try saying:", '[dim]"List the files in ~/project on the remote server"[/]')
    table.add_row("", '[dim]"Run make test on the remote"[/]')
    table.add_row("", '[dim]"Show me the git diff on the remote"[/]')

    console.print()
    console.print(Panel(table, title="[bold green]Ready to go![/]", border_style="green"))


# ── Main ─────────────────────────────────────────────────────────────────────

def run_setup():
    step_welcome()

    if not step_prereqs():
        sys.exit(1)

    host_info = step_configure_host()
    alias = host_info["alias"]
    host_config = host_info["host_config"]

    step_write_config(alias, host_config)
    step_ssh_config(alias, host_config)
    step_register_mcp()
    step_test_connection(alias)
    step_done(alias)


def main():
    try:
        run_setup()
    except KeyboardInterrupt:
        console.print("\n[dim]Setup cancelled.[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
