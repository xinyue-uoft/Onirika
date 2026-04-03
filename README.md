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

### Option A: Setup Wizard

```bash
git clone https://github.com/xinyue-uoft/Onirika.git
cd Onirika
uv sync
uv run onirika-setup
```

The wizard walks through host configuration, SSH setup, MCP registration, and connection testing.

### Option B: Manual Setup

**1. Create config**

```bash
mkdir -p ~/.config/onirika
cp config.example.yaml ~/.config/onirika/config.yaml
# Edit with your server details
```

**2. Register with Claude Code**

```bash
claude mcp add --scope user onirika-ssh \
  -- uv --directory /path/to/Onirika run onirika-ssh
```

**3. Authenticate**

```bash
# Get Kerberos ticket (if applicable)
kinit

# Open SSH master connection (handles 2FA)
ssh -M -S ~/.ssh/ctrl-%r@%h:%p user@server
```

**4. Use Claude Code**

```bash
claude
```

Then ask: "List the files in ~/project on the remote server" or "Run make test on the remote".

**5. Or use the tmux launcher** (does steps 3-4 together)

```bash
./bin/onirika-launch myserver
```

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
./bin/onirika-launch <host-alias>

# Clean up stale sessions and temp files
./bin/onirika-launch --clean <host-alias>
```

Click on a pane to switch focus (mouse mode is enabled).

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
```

### Adding a New Tool

1. Add the function in the appropriate `onirika/tools/*.py` module
2. Decorate with `@mcp.tool()`
3. Use `require_connection(host)` to get an executor
4. Use `shlex.quote()` on all user-provided values embedded in commands
5. Validate non-string parameters with explicit type casts (`int()`, allowlists)

## License

AGPL-3.0 -- see [LICENSE](LICENSE).
