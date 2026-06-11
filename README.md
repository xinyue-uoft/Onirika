# Onirika

An MCP (Model Context Protocol) server that gives Claude Code -- and any MCP-compatible AI tool -- the ability to read files, write files, execute commands, search code, and manage git on remote SSH servers. Everything runs locally; nothing is installed on the remote. Authentication (Kerberos, 2FA, SSH keys) is handled through SSH ControlMaster, keeping credentials out of the MCP transport entirely.

## Architecture

```
┌─────────────┐     MCP (stdio)     ┌──────────────┐
│ Claude Code  │ ◄──────────────────► │  Onirika MCP │
│   (local)    │    tool calls/       │   Server     │
└─────────────┘    results            └──────┬───────┘
                                             │
                                    SSH ControlMaster
                                      (piggybacks on
                                    existing session)
                                             │
                                      ┌──────▼───────┐
                                      │ Remote Server │
                                      │ (Linux/bash)  │
                                      └──────────────┘
```

**How it works:** You authenticate once in a terminal (handling kinit, 2FA, etc.), which creates an SSH ControlMaster socket. Onirika piggybacks on that socket for all operations -- it never sees your credentials.

## Features

- **18 MCP tools** for files, commands, search, and git -- all via SSH
- **Zero remote installation** -- uses only standard Unix tools (cat, sed, find, grep, git)
- **Any SSH auth method** -- Kerberos/GSSAPI, 2FA, SSH keys, passwords, ProxyJump
- **Atomic file writes** -- temp file + mv prevents partial writes
- **Background jobs** -- long-running commands (builds, test suites) with status polling
- **Environment preamble** -- source setup scripts before every command
- **Binary file detection** -- refuses to read binary files, suggests alternatives
- **Output truncation** -- prevents memory exhaustion from runaway commands
- **Interactive setup wizard** -- imports hosts from `~/.ssh/config`
- **tmux launcher** -- opens SSH master + Claude Code side by side

## Requirements

- Python 3.10+
- OpenSSH client (`ssh`)
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- tmux (optional, for the launcher)
- Claude Code CLI (optional, for MCP registration)

## Quick Start

```bash
git clone https://github.com/xinyue-uoft/Onirika.git
cd Onirika
uv tool install .
onirika setup                     # configure host + register MCP with Claude Code
onirika launch <host-alias>       # tmux + SSH master + Claude Code (needs tmux)
```

`uv tool install .` installs the package into its own isolated environment and puts four commands on your PATH:

| Command | What it does |
|---------|--------------|
| `onirika` | Top-level dispatcher (`setup`, `ssh`, `launch`, `web`) |
| `onirika-ssh` | MCP server entry point (what Claude Code spawns) |
| `onirika-setup` | Same wizard as `onirika setup` |
| `onirika-web` | Same web UI as `onirika web` |

Reinstall after pulling updates: `uv tool install --reinstall .`. Uninstall: `uv tool uninstall onirika-ssh`. For an editable install that tracks your working tree: `uv tool install --editable .`.

`onirika launch` requires `tmux` and runs only on macOS/Linux. Everything else (the MCP server, setup wizard, web UI) is pure Python and works on Windows too.

### Manual MCP registration

The wizard registers `onirika-ssh` automatically. If you want to do it yourself:

```bash
claude mcp add --scope user onirika-ssh -- onirika-ssh
```

Do **not** register `uv run onirika-ssh` — the dependency resolution on cold start can exceed Claude Code's MCP handshake timeout and surface as `Failed to connect` even when the server is healthy. The bare `onirika-ssh` command (provided by `uv tool install`) starts in milliseconds.

### Using with opencode

`onirika-ssh` is a standard stdio MCP server, so it plugs into [opencode](https://opencode.ai) too. Add an entry to `~/.config/opencode/opencode.json` (or the project-local `opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "onirika-ssh": {
      "type": "local",
      "command": ["onirika-ssh"],
      "enabled": true
    }
  }
}
```

Then launch with opencode as the right-pane agent:

```bash
onirika launch --agent opencode myserver
# or set the default:
export ONIRIKA_AGENT=opencode
onirika launch myserver
```

### Configure without the wizard

```bash
mkdir -p ~/.config/onirika
cp config.example.yaml ~/.config/onirika/config.yaml
# Edit with your server details, then register MCP as shown above.
```

### Authenticate and start Claude Code

```bash
# Get Kerberos ticket (if applicable)
kinit

# Open SSH master connection (handles 2FA)
ssh -M -S ~/.ssh/ctrl-%r@%h:%p user@server

# In another terminal:
claude
```

Then ask: "List the files in ~/project on the remote server" or "Run make test on the remote".

Or do all of the above in one shot with the tmux launcher:

```bash
onirika launch myserver
```

## Local SSH Gateway (for Claude Code's native SSH)

Claude Code can connect to a remote machine over its built-in SSH support, but a
direct connection trips over Kerberos / 2FA / `ProxyCommand` prompts — there
is no TTY in which to complete them. The `onirika proxy` gateway sidesteps this:
it exposes a stable, key-only SSH endpoint on `127.0.0.1:4242` (loopback only —
never visible on the LAN) and bridges every channel to the real host through an
already-established ControlMaster socket. The hard authentication is paid once,
interactively; the gateway is a frictionless door behind it.

```bash
# Terminal A — open the master (Kerberos/2FA happens here), keep it alive:
onirika establish myserver

# Terminal B — start the gateway:
onirika proxy myserver

# ...or do both in one command (auth still interactive, master backgrounded):
onirika proxy myserver --establish
```

This writes a fixed `onirika-local-host` alias into `~/.ssh/config`. In Claude
Code's "Edit SSH connection" dialog, set the **SSH Host** to `onirika-local-host`
— it connects with no Kerberos and no 2FA. The alias never changes; to point it
at a different server, just re-run `onirika proxy <other-host>`.

```bash
onirika proxy status   # is the alias present / the port listening?
onirika proxy stop      # remove the ssh-config alias (the master is left alone)
```

### Scoping a proxy / env to the agent only (`--source`)

Some remote hosts can only reach the internet through a proxy that you enable by
sourcing a script (e.g. an institutional `web_proxy.sh` that exports
`HTTPS_PROXY`). You usually *don't* want to source it into your login shell —
that would route everything (including local-service and internal tests) through
the proxy. Instead, scope it to just the commands the gateway forwards:

```bash
onirika proxy myserver --establish --source ~admin/bin/web_proxy.sh
```

The gateway prepends `source <script>;` to every exec command it forwards, so the
remote Claude Code server — and the agent children it spawns — inherit the proxy
env, while your interactive shell and anything you run outside the gateway stay
clean. Loopback (`127.0.0.1`/`localhost`/`::1`) is auto-added to `NO_PROXY`.
`--source` is repeatable. Combine with the script's own `NO_PROXY` (e.g. your
internal domain like `.internal.example.com`) so internal hosts bypass the proxy
and only external API calls go through it.

Press `Ctrl+C` in Terminal B to tear the gateway down; it removes its own
`~/.ssh/config` alias and `~/.ssh/known_hosts` entry but never touches the
ControlMaster — that belongs to whoever ran `onirika establish`.

**Host key note.** Claude Code verifies the gateway's host key against
`~/.ssh/known_hosts` directly — it ignores `StrictHostKeyChecking` /
`UserKnownHostsFile` from ssh_config and has no trust-on-first-use prompt. The
gateway therefore uses a *persistent* host key (`~/.local/share/onirika/host_key`)
and registers it in `~/.ssh/known_hosts` on startup, removing it on teardown.
This is why the alias works in Claude Code and not just from the terminal.

## Tool Reference

### Connection

| Tool | Description |
|------|-------------|
| `ssh_check_connection` | Check if the SSH ControlMaster socket is alive |

### Commands

| Tool | Description |
|------|-------------|
| `ssh_run` | Execute a shell command (with environment preamble) |
| `ssh_run_background` | Start a long-running command via nohup |
| `ssh_job_status` | Check progress and output of a background job |

### Files

| Tool | Description |
|------|-------------|
| `ssh_read_file` | Read lines from a file (supports offset/limit) |
| `ssh_write_file` | Write content to a file (atomic temp+mv) |
| `ssh_patch_file` | Find-and-replace a unique string in a file |
| `ssh_list_dir` | List directory contents (recursive optional) |
| `ssh_file_info` | Get file metadata (size, permissions, mtime) |
| `ssh_mkdir` | Create directories |
| `ssh_move` | Move or rename files/directories |
| `ssh_delete` | Delete files or directories |
| `ssh_download` | Download a remote file to local machine |

### Search

| Tool | Description |
|------|-------------|
| `ssh_grep` | Search file contents with regex (like grep -rn) |
| `ssh_find_files` | Find files by name pattern (like find -name) |

### Git

| Tool | Description |
|------|-------------|
| `ssh_git_status` | Branch, staged, modified, and untracked files |
| `ssh_git_diff` | Show diff (working tree, staged, or between refs) |
| `ssh_git_log` | Commit history with hash, author, date, message |

## Configuration

Config file location: `~/.config/onirika/config.yaml`

```yaml
# Which host to use when no host parameter is specified
default_host: myserver

hosts:
  myserver:
    # SSH hostname or alias (must match ssh config or be resolvable)
    ssh_host: server.example.com

    # SSH username (optional -- uses system default if omitted)
    user: jdoe

    # SSH port (default: 22)
    port: 22

    # Path to ControlMaster socket (supports SSH tokens: %r, %h, %p)
    control_path: "~/.ssh/ctrl-%r@%h:%p"

    # Shell commands sourced before every remote command
    preamble: |
      source ~/project/env.sh

    # Default working directory for commands
    default_cwd: "~/project"

    # Timeout for commands in seconds (default: 30)
    command_timeout: 30

    # Timeout for file operations in seconds (default: 60)
    file_timeout: 60

    # Maximum file size for read/write in bytes (default: 10MB)
    max_file_size: 10485760

    # Maximum output lines for commands (default: 5000)
    max_output_lines: 5000
```

## Launcher

The tmux launcher opens two panes side by side:
- **Left pane**: Interactive SSH master connection (handles kinit, 2FA)
- **Right pane**: Waits for connection, then starts Claude Code

```bash
onirika launch <host-alias>

# Clean up stale sessions and temp files
onirika launch --clean <host-alias>
```

Click on a pane to switch focus (mouse mode is enabled). The launcher requires `tmux` (macOS/Linux only).

## Authentication Model

Onirika never handles authentication directly. Instead:

1. **You authenticate** in a terminal -- run `kinit`, connect via `ssh`, complete any 2FA prompts
2. **SSH creates a ControlMaster socket** -- a Unix domain socket that other SSH processes can reuse
3. **Onirika piggybacks** on that socket -- all its SSH commands use `-S <socket>` to reuse the authenticated session

This means:
- Credentials never pass through the MCP protocol
- Any auth method SSH supports works (Kerberos, GSSAPI, 2FA, keys, passwords, ProxyJump)
- If the session drops, Onirika returns a clear error telling you to re-authenticate

## Development

```bash
git clone https://github.com/xinyue-uoft/Onirika.git
cd Onirika
uv sync --extra dev

# Run unit tests
uv run pytest tests/ -v --ignore=tests/integration_test.py

# Run integration tests (requires live SSH connection)
uv run python tests/integration_test.py [host-alias]

# Test MCP server starts correctly
echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}' | uv run onirika-ssh

# Run any subcommand from the source tree
uv run onirika launch <host>
```

For an editable global install that tracks your working tree, use `uv tool install --editable .`. `onirika setup` detects whether you have a global install, a project venv, or only `uv run` and registers the right MCP command accordingly.

### Adding a New Tool

1. Add the function in the appropriate `onirika/tools/*.py` module
2. Decorate with `@mcp.tool()`
3. Use `require_connection(host)` to get an executor
4. Use `shlex.quote()` on all user-provided values embedded in commands
5. Validate non-string parameters with explicit type casts (`int()`, allowlists)

## License

AGPL-3.0 -- see [LICENSE](LICENSE).
