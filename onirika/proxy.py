"""The local SSH gateway — Onirika's transparent door for Claude Code.

Claude Code points at ``onirika-local-host`` (127.0.0.1:4242) and connects with
a key-only handshake — no Kerberos, no 2FA, no TTY prompt. Every channel it
opens is bridged to the *real* remote host by spawning an ``ssh`` subprocess
that piggybacks on an already-established ControlMaster socket
(``ssh -S <socket> -o ControlMaster=no``). The hard authentication was paid for
once, by ``onirika establish``; this gateway only re-uses the open tunnel.

The bridge is byte-transparent (the server runs with ``encoding=None``), so
shell sessions, PTY control sequences, and exec output all pass through
untouched. PTY mode allocates a local pseudo-terminal so interactive programs
on the far side behave, and window-resize events are forwarded via TIOCSWINSZ.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import struct
import termios
from pathlib import Path

import asyncssh

from onirika.config import HostConfig

DEFAULT_PORT = 4242
_READ_SIZE = 65536


def _set_winsize(fd: int, width: int, height: int, pixwidth: int, pixheight: int) -> None:
    """Push a terminal window size onto a pty master via ioctl.

    TIOCSWINSZ expects (rows, cols, xpixels, ypixels); asyncssh hands us
    (width=cols, height=rows, pixwidth, pixheight).
    """
    winsize = struct.pack("HHHH", height, width, pixwidth, pixheight)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


class OnirikaSSHServer(asyncssh.SSHServer):
    """Auth policy for the gateway: public-key only, against the gateway key.

    The actual key comparison is done by asyncssh against the
    ``authorized_client_keys`` file passed to ``create_server``; here we just
    declare that public-key auth is required and supported, and mint a session
    object per channel.
    """

    def __init__(self, host_config: HostConfig, preamble: str = ""):
        self._cfg = host_config
        self._preamble = preamble

    def begin_auth(self, username: str) -> bool:
        # Returning True means authentication is required (no anonymous access).
        return True

    def public_key_auth_supported(self) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return False

    def session_requested(self) -> "OnirikaSSHSession":
        return OnirikaSSHSession(self._cfg, self._preamble)


class OnirikaSSHSession(asyncssh.SSHServerSession):
    """Bridges one client channel to an ``ssh`` subprocess over the master socket."""

    def __init__(self, host_config: HostConfig, preamble: str = ""):
        self._cfg = host_config
        self._preamble = preamble
        self._chan: asyncssh.SSHServerChannel | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._pty_master: int | None = None
        self._want_pty = False
        self._term_size = (80, 24, 0, 0)
        self._command: str | None = None
        self._subsystem: str | None = None
        self._run_task: asyncio.Task | None = None
        # session_started schedules _run as a task, but asyncssh may deliver
        # data_received / eof_received before that task creates the subprocess.
        # Buffer client input until the writer (pty master or proc stdin) exists.
        self._writer_ready = False
        self._inbuf = bytearray()
        self._client_eof = False

    # ---- channel lifecycle -------------------------------------------------

    def connection_made(self, chan: asyncssh.SSHServerChannel) -> None:
        self._chan = chan

    def pty_requested(self, term_type, term_size, term_modes) -> bool:
        self._want_pty = True
        # term_size is (width, height, pixwidth, pixheight)
        self._term_size = term_size
        return True

    def shell_requested(self) -> bool:
        self._command = None
        return True

    def exec_requested(self, command: str) -> bool:
        self._command = command
        return True

    def subsystem_requested(self, subsystem: str) -> bool:
        # Claude Code opens the `sftp` subsystem to browse/sync the remote
        # filesystem. Forward it to the real host's subsystem over the master.
        self._subsystem = subsystem
        return True

    def session_started(self) -> None:
        self._run_task = asyncio.ensure_future(self._run())

    def terminal_size_changed(self, width, height, pixwidth, pixheight) -> None:
        self._term_size = (width, height, pixwidth, pixheight)
        if self._pty_master is not None:
            _set_winsize(self._pty_master, width, height, pixwidth, pixheight)

    def data_received(self, data: bytes, datatype) -> None:
        # Client -> remote. Buffer until the subprocess writer is ready.
        if not self._writer_ready:
            self._inbuf += data
            return
        self._write_to_remote(data)

    def eof_received(self) -> bool:
        # Client closed its stdin; propagate to the subprocess (pipe mode only).
        # Return True to keep the channel half-open: the client may have sent EOF
        # (e.g. `ssh host cmd < input`) while the command still has output to send
        # back. Returning False/None would let asyncssh close the channel outright.
        if not self._writer_ready:
            self._client_eof = True
            return True
        self._send_remote_eof()
        return True

    def _write_to_remote(self, data: bytes) -> None:
        if self._pty_master is not None:
            try:
                os.write(self._pty_master, data)
            except OSError:
                pass
        elif self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(data)
            except (OSError, BrokenPipeError):
                pass

    def _send_remote_eof(self) -> None:
        # PTY has no separate EOF channel; only pipe-mode stdin can be closed.
        if self._pty_master is None and self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write_eof()
            except OSError:
                pass

    def _flush_buffered_input(self) -> None:
        """Called once the subprocess writer exists: drain buffered stdin + EOF."""
        self._writer_ready = True
        if self._inbuf:
            self._write_to_remote(bytes(self._inbuf))
            self._inbuf.clear()
        if self._client_eof:
            self._send_remote_eof()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cleanup()

    # ---- the bridge --------------------------------------------------------

    def _build_argv(self) -> list[str]:
        cp = self._cfg.resolved_control_path
        argv = [
            "ssh",
            "-S", cp,
            "-o", "ControlMaster=no",
            "-o", "BatchMode=yes",
        ]
        if self._cfg.port != 22:
            argv += ["-p", str(self._cfg.port)]
        if self._subsystem is not None:
            # Mirror OpenSSH's own sftp client: `ssh <opts> -s -- <host> <subsystem>`.
            # -s requests subsystem invocation; -- ends option parsing.
            argv += ["-s", "--", self._cfg.ssh_target, self._subsystem]
            return argv
        if self._want_pty:
            # Force remote PTY allocation regardless of the subprocess's local tty.
            argv.append("-tt")
        argv.append(self._cfg.ssh_target)
        if self._command is not None:
            cmd = self._command
            if self._preamble:
                # Scope env (e.g. an HTTP proxy script) to forwarded commands only.
                # The remote server Claude Code launches inherits this; so do the
                # ccd-cli children it spawns. The user's own shell never sees it.
                cmd = f"{self._preamble}; {cmd}"
            argv.append(cmd)
        return argv

    async def _run(self) -> None:
        argv = self._build_argv()
        try:
            if self._want_pty:
                await self._run_pty(argv)
            else:
                await self._run_pipe(argv)
        except Exception as e:  # noqa: BLE001 — surface any bridge failure to the client
            if self._chan is not None:
                self._chan.write_stderr(f"onirika gateway: {e}\n".encode())
                try:
                    self._chan.exit(255)
                except OSError:
                    pass

    async def _run_pty(self, argv: list[str]) -> None:
        loop = asyncio.get_event_loop()
        master, slave = pty.openpty()
        self._pty_master = master
        w, h, pw, ph = self._term_size
        _set_winsize(master, w, h, pw, ph)

        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)  # child owns the only slave fd now
        self._flush_buffered_input()

        eof = loop.create_future()

        def _on_master_readable() -> None:
            try:
                data = os.read(master, _READ_SIZE)
            except OSError:
                data = b""
            if data:
                if self._chan is not None:
                    self._chan.write(data)
            else:
                loop.remove_reader(master)
                if not eof.done():
                    eof.set_result(None)

        loop.add_reader(master, _on_master_readable)

        rc = await self._proc.wait()
        await eof  # flush everything buffered in the pty before signalling exit
        loop.remove_reader(master)
        self._finish(rc)

    async def _run_pipe(self, argv: list[str]) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._flush_buffered_input()

        async def pump(reader: asyncio.StreamReader, is_stderr: bool) -> None:
            while True:
                chunk = await reader.read(_READ_SIZE)
                if not chunk:
                    break
                if self._chan is None:
                    break
                if is_stderr:
                    self._chan.write_stderr(chunk)
                else:
                    self._chan.write(chunk)

        await asyncio.gather(
            pump(self._proc.stdout, False),
            pump(self._proc.stderr, True),
        )
        rc = await self._proc.wait()
        self._finish(rc)

    def _finish(self, rc: int | None) -> None:
        if self._chan is None:
            return
        try:
            self._chan.write_eof()
        except OSError:
            pass
        try:
            self._chan.exit(rc if rc is not None and rc >= 0 else 255)
        except OSError:
            pass

    def _cleanup(self) -> None:
        if self._pty_master is not None:
            try:
                os.close(self._pty_master)
            except OSError:
                pass
            self._pty_master = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass


async def start_proxy(
    host_config: HostConfig,
    pub_path: Path,
    port: int = DEFAULT_PORT,
    host_key: asyncssh.SSHKey | None = None,
    preamble: str = "",
) -> asyncssh.SSHAcceptor:
    """Start the gateway listening on 127.0.0.1:<port>.

    Args:
        host_config: the target remote, whose ControlMaster socket is reused.
        pub_path: path to the gateway public key (authorized_client_keys).
        port: loopback port to bind (default 4242).
        host_key: server host key to present. The CLI passes a *persistent* key
            (see proxy_key.ensure_host_key) so it can be registered in
            known_hosts; an ephemeral one is generated only if omitted (e.g. in
            tests, where the client uses known_hosts=None).
        preamble: shell snippet prepended (``<preamble>; <command>``) to every
            forwarded exec command — e.g. sourcing a proxy script. Scoped to the
            agent's processes only; the remote login shell is untouched.

    Returns the SSHAcceptor; call ``.close()`` + ``await .wait_closed()`` to stop.
    """
    if host_key is None:
        host_key = asyncssh.generate_private_key("ssh-ed25519")

    return await asyncssh.create_server(
        lambda: OnirikaSSHServer(host_config, preamble),
        "127.0.0.1",
        port,
        server_host_keys=[host_key],
        authorized_client_keys=str(pub_path),
        encoding=None,  # byte-transparent: no utf-8 mangling of control sequences
        line_editor=False,  # transparent pipe — never intercept/echo input lines
    )
