"""FastAPI web app — serves terminals via WebSocket and static frontend."""

from __future__ import annotations

import asyncio
import os
import shlex
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from onirika.config import get_config
from onirika.web.pty_bridge import PtyManager

STATIC_DIR = Path(__file__).parent / "static"

pty_manager = PtyManager()

# Host alias, set by __main__ before app starts
_host_alias: str | None = None
_auto_open: bool = True


def _build_ssh_script() -> str:
    """Build an interactive shell script for the SSH master pane."""
    config = get_config()
    host = config.get_host(_host_alias)
    target = host.ssh_target
    control_path = host.resolved_control_path

    # Shell script that guides the user through auth, then connects
    return (
        f'echo ""\n'
        f'echo "  Onirika SSH Master"\n'
        f'echo "  Target: {target}"\n'
        f'echo ""\n'
        # Check for Kerberos ticket
        f'if command -v klist >/dev/null 2>&1 && klist -s 2>/dev/null; then\n'
        f'  echo "  Kerberos ticket: OK"\n'
        f'else\n'
        f'  echo "  No Kerberos ticket found."\n'
        f'  echo ""\n'
        f'  printf "  Run kinit? [Y/n] "\n'
        f'  read -n 1 REPLY\n'
        f'  echo ""\n'
        f'  if [ "$REPLY" != "N" ] && [ "$REPLY" != "n" ]; then\n'
        f'    kinit\n'
        f'  fi\n'
        f'fi\n'
        f'echo ""\n'
        f'echo "  Connecting..."\n'
        f'echo ""\n'
        f'ssh -M -S {shlex.quote(control_path)} {shlex.quote(target)}\n'
        f'echo ""\n'
        f'echo "  SSH session ended. Press Enter to reconnect, or type exit."\n'
        f'while read -r line; do\n'
        f'  if [ "$line" = "exit" ]; then break; fi\n'
        f'  ssh -M -S {shlex.quote(control_path)} {shlex.quote(target)}\n'
        f'  echo "  SSH session ended. Press Enter to reconnect, or type exit."\n'
        f'done\n'
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: spawn terminals. Shutdown: clean up."""
    # Spawn SSH session — interactive bash script that handles auth
    ssh_script = _build_ssh_script()
    await pty_manager.create("ssh", "bash", ["-c", ssh_script])

    # Spawn Claude session
    await pty_manager.create("claude", "claude", [])

    if _auto_open:
        port = app.state.port if hasattr(app.state, "port") else 8765
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass  # Headless environments — user opens manually

    yield

    await pty_manager.close_all()


app = FastAPI(title="Onirika Web", lifespan=lifespan)

# Serve static files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Pages ────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ── WebSocket terminal endpoint ──────────────────────────────────────────────

@app.websocket("/ws/terminal/{session_id}")
async def terminal_ws(websocket: WebSocket, session_id: str):
    await websocket.accept()
    session = pty_manager.get(session_id)
    if not session:
        await websocket.close(code=4004, reason="Session not found")
        return

    session.websocket = websocket
    read_task = asyncio.create_task(session.pump_pty_to_ws())

    try:
        while True:
            data = await websocket.receive_bytes()
            session.write(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        read_task.cancel()
        session.websocket = None


# ── REST API ─────────────────────────────────────────────────────────────────

class ResizeRequest(BaseModel):
    cols: int
    rows: int


@app.post("/api/terminal/{session_id}/resize")
async def resize_terminal(session_id: str, req: ResizeRequest):
    session = pty_manager.get(session_id)
    if not session:
        return JSONResponse({"error": "not_found"}, status_code=404)
    session.resize(req.cols, req.rows)
    return {"ok": True}


@app.get("/api/status")
async def status():
    """Check which sessions are alive."""
    sessions = {}
    for sid, session in pty_manager.sessions.items():
        try:
            os.waitpid(session.pid, os.WNOHANG)
            alive = True
        except ChildProcessError:
            alive = False
        sessions[sid] = {"pid": session.pid, "alive": alive}
    return {"sessions": sessions}


