#!/usr/bin/env bash
# Onirika launcher — opens a tmux session with SSH master + AI agent side by side.
#
# Usage:
#   onirika-launch [--agent claude|opencode] [--clean] <host-alias>
#
# The right pane runs whichever agent is selected. Default is `claude`; pass
# `--agent opencode` or set ONIRIKA_AGENT=opencode to switch.
#
# Left pane:  Interactive shell for kinit + SSH master connection.
# Right pane: AI agent (starts after SSH master is up).

set -euo pipefail

# ── Parse args ───────────────────────────────────────────────────────────────
CLEAN=false
HOST_ALIAS=""
AGENT="${ONIRIKA_AGENT:-claude}"

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=true; shift ;;
        --agent) AGENT="$2"; shift 2 ;;
        --agent=*) AGENT="${1#--agent=}"; shift ;;
        -*)      echo "Unknown flag: $1"; echo "Usage: onirika-launch [--agent claude|opencode] [--clean] <host-alias>"; exit 1 ;;
        *)       HOST_ALIAS="$1"; shift ;;
    esac
done

if [ -z "$HOST_ALIAS" ]; then
    echo "Usage: onirika-launch [--agent claude|opencode] [--clean] <host-alias>"
    exit 1
fi

# Verify the chosen agent binary exists before doing all the tmux setup.
if ! command -v "$AGENT" &>/dev/null; then
    echo "Error: agent binary '$AGENT' not found on PATH."
    echo "Install it first, or pick another with --agent."
    exit 1
fi

SESSION_NAME="onirika-${HOST_ALIAS}"

# Pick the Python that has pyyaml. Caller (onirika.cli) sets ONIRIKA_PYTHON
# to its own interpreter; fall back to system python3 for direct invocation.
PY="${ONIRIKA_PYTHON:-python3}"

# Read config
CONFIG_FILE="${ONIRIKA_CONFIG:-$HOME/.config/onirika/config.yaml}"
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Config file not found: $CONFIG_FILE"
    echo "Run 'onirika setup' first, or create the config manually."
    exit 1
fi

# Extract SSH target and control path from config (pass vars as args, not interpolated)
read -r SSH_HOST SSH_TARGET CONTROL_PATH <<< "$("$PY" - "$CONFIG_FILE" "$HOST_ALIAS" << 'PYEOF'
import yaml, os, sys
config_file, host_alias = sys.argv[1], sys.argv[2]
with open(config_file) as f:
    cfg = yaml.safe_load(f)
host = cfg.get('hosts', {}).get(host_alias, {})
user = host.get('user', '')
ssh_host = host.get('ssh_host', host_alias)
target = f'{user}@{ssh_host}' if user else ssh_host
cp = os.path.expanduser(host.get('control_path', '~/.ssh/ctrl-%r@%h:%p'))
print(ssh_host, target, cp)
PYEOF
)" || { echo "Failed to read config"; exit 1; }

# ── Cleanup ──────────────────────────────────────────────────────────────────
# Always clean stale artifacts from previous runs.
tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
rm -f /tmp/onirika-left.* /tmp/onirika-right.* 2>/dev/null || true

if [ "$CLEAN" = true ]; then
    echo "Cleaned stale sessions and temp files."
    # Also kill any other onirika tmux sessions
    tmux list-sessions -F '#{session_name}' 2>/dev/null | grep '^onirika-' | while read -r s; do
        tmux kill-session -t "$s" 2>/dev/null || true
    done
    echo "Done. Re-run without --clean to launch."
    exit 0
fi

# ── Create temp scripts ─────────────────────────────────────────────────────
LEFT_SCRIPT=$(mktemp /tmp/onirika-left.XXXXXXXX)
cat > "$LEFT_SCRIPT" << 'LEFTEOF'
#!/usr/bin/env bash
SSH_TARGET="$1"
SSH_HOST="$2"
CONTROL_PATH="$3"

clear
echo ""
echo "  ══════════════════════════════════════════"
echo "  ║       Onirika SSH Master Pane          ║"
echo "  ══════════════════════════════════════════"
echo ""
echo "  Target: $SSH_TARGET"
echo ""
echo "  ── Step 1: Authenticate ──"
echo ""

# Check if Kerberos ticket exists
if command -v klist &>/dev/null && klist -s 2>/dev/null; then
    echo "  ✓ Kerberos ticket found."
else
    echo "  No active Kerberos ticket."
    echo ""
    read -p "  Run kinit? [Y/n] " -n 1 -r REPLY
    echo ""
    if [[ ! "$REPLY" =~ ^[Nn]$ ]]; then
        kinit
        echo ""
    fi
fi

echo ""
echo "  ── Step 2: SSH Master Connection ──"
echo ""
echo "  Connecting to $SSH_TARGET..."
echo "  (Complete any 2FA prompts below)"
echo ""

ssh -M -S "$CONTROL_PATH" "$SSH_TARGET"

# If SSH exits, show a message
echo ""
echo "  SSH session ended. Press Enter to reconnect, or Ctrl-D to exit."
while read -r; do
    ssh -M -S "$CONTROL_PATH" "$SSH_TARGET"
    echo ""
    echo "  SSH session ended. Press Enter to reconnect, or Ctrl-D to exit."
done
LEFTEOF
chmod +x "$LEFT_SCRIPT"

# Write helper for right pane — waits for connection before starting the agent
RIGHT_SCRIPT=$(mktemp /tmp/onirika-right.XXXXXXXX)
cat > "$RIGHT_SCRIPT" << 'RIGHTEOF'
#!/usr/bin/env bash
SSH_TARGET="$1"
CONTROL_PATH="$2"
AGENT="$3"

clear
echo ""
echo "  Waiting for SSH master connection..."
echo "  (Complete authentication in the left pane)"
echo ""

# Poll for the control socket to become active
for i in $(seq 1 120); do
    if ssh -O check -S "$CONTROL_PATH" "$SSH_TARGET" 2>/dev/null; then
        echo ""
        echo "  ✓ SSH connection active! Starting $AGENT..."
        echo ""
        sleep 1
        exec "$AGENT"
    fi
    sleep 2
done

echo ""
echo "  ✗ Timed out waiting for SSH connection (4 minutes)."
echo "  Start the agent manually: $AGENT"
echo ""
exec bash
RIGHTEOF
chmod +x "$RIGHT_SCRIPT"

# Create tmux session
tmux new-session -d -s "$SESSION_NAME" -n main

# Left pane: interactive SSH setup
tmux send-keys -t "$SESSION_NAME" "bash '$LEFT_SCRIPT' '$SSH_TARGET' '$SSH_HOST' '$CONTROL_PATH'; rm -f '$LEFT_SCRIPT'" Enter

# Right pane: wait for connection, then start the agent
tmux split-window -h -t "$SESSION_NAME"
tmux send-keys -t "$SESSION_NAME" "bash '$RIGHT_SCRIPT' '$SSH_TARGET' '$CONTROL_PATH' '$AGENT'; rm -f '$RIGHT_SCRIPT'" Enter

# Focus the left pane (user authenticates there first)
tmux select-pane -t "$SESSION_NAME":0.0

# Enable mouse — click to switch panes, scroll to see history
tmux set-option -t "$SESSION_NAME" mouse on

# Show navigation hint
tmux set-option -t "$SESSION_NAME" status-right "  click pane to switch | Ctrl-b q show pane #  "

# Attach
tmux attach-session -t "$SESSION_NAME"
