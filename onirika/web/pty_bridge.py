"""PTY-to-WebSocket bridge — spawns processes and streams their I/O."""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.websockets import WebSocket


@dataclass
class PtySession:
    """A single PTY session bridging a subprocess to a WebSocket."""

    session_id: str
    master_fd: int
    pid: int
    _proc: subprocess.Popen | None = None
    websocket: WebSocket | None = None
    _read_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=128))
    _closed: bool = False

    async def start_reader(self):
        """Register the master fd with the event loop for readable events."""
        loop = asyncio.get_running_loop()
        loop.add_reader(self.master_fd, self._on_pty_readable)

    def _on_pty_readable(self):
        """Callback when PTY master has data. Runs in the event loop thread."""
        try:
            data = os.read(self.master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            try:
                self._read_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return

        if not data:
            try:
                self._read_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return

        try:
            self._read_queue.put_nowait(data)
        except asyncio.QueueFull:
            # Backpressure: drop oldest to prevent memory buildup
            try:
                self._read_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._read_queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def pump_pty_to_ws(self):
        """Drain the read queue and send data to the WebSocket."""
        while not self._closed:
            data = await self._read_queue.get()
            if data is None:
                if self.websocket:
                    try:
                        await self.websocket.send_json({
                            "type": "exit",
                            "message": "Process exited",
                        })
                    except Exception:
                        pass
                break
            if self.websocket:
                try:
                    await self.websocket.send_bytes(data)
                except Exception:
                    break

    def write(self, data: bytes):
        """Write input from WebSocket to the PTY."""
        if not self._closed:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def resize(self, cols: int, rows: int):
        """Resize the PTY. The child process receives SIGWINCH."""
        if not self._closed:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            try:
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    async def close(self):
        """Clean up: remove fd reader, close fd, terminate and reap process."""
        if self._closed:
            return
        self._closed = True
        try:
            loop = asyncio.get_running_loop()
            loop.remove_reader(self.master_fd)
        except Exception:
            pass
        try:
            os.close(self.master_fd)
        except OSError:
            pass
        # Terminate process and reap to prevent zombies
        if self._proc:
            try:
                self._proc.terminate()
                # Give it a moment to exit gracefully
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
            except Exception:
                pass
        else:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class PtyManager:
    """Manages multiple PTY sessions."""

    def __init__(self):
        self.sessions: dict[str, PtySession] = {}

    async def create(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        cols: int = 80,
        rows: int = 24,
        env: dict[str, str] | None = None,
    ) -> PtySession:
        """Spawn a process in a new PTY and register it."""
        args = args or []
        master_fd, slave_fd = pty.openpty()

        # Set initial window size before spawning
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

        # Make master fd non-blocking for add_reader
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Build environment
        proc_env = os.environ.copy()
        proc_env["TERM"] = "xterm-256color"
        proc_env["COLORTERM"] = "truecolor"
        if env:
            proc_env.update(env)

        # Spawn process with slave as controlling terminal
        proc = subprocess.Popen(
            [command] + args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=proc_env,
            close_fds=True,
        )

        # Close slave in parent — child owns it
        os.close(slave_fd)

        session = PtySession(
            session_id=session_id,
            master_fd=master_fd,
            pid=proc.pid,
            _proc=proc,
        )
        await session.start_reader()
        self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> PtySession | None:
        return self.sessions.get(session_id)

    async def close_all(self):
        """Shut down all sessions."""
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()

    async def close(self, session_id: str):
        """Close a single session."""
        session = self.sessions.pop(session_id, None)
        if session:
            await session.close()
