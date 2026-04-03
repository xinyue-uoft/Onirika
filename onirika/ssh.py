"""SSH executor — runs commands on remote hosts via ControlMaster sockets."""

from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass

from onirika.config import HostConfig

# How long a successful connection check remains valid (seconds).
_CHECK_CACHE_TTL = 5.0


@dataclass
class SSHResult:
    """Result of a remote SSH command execution."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


class SSHExecutor:
    """Executes commands on a remote host via an existing SSH ControlMaster socket.

    Requires the user to have established a ControlMaster connection beforehand
    (e.g. via `ssh -M -S <socket> user@host`). This class never handles
    authentication — it piggybacks on the existing session.
    """

    def __init__(self, host_config: HostConfig):
        self.config = host_config
        self._control_path = host_config.resolved_control_path
        self._last_check: float = 0.0
        self._last_check_ok: bool = False

    async def check_connection(self, force: bool = False) -> tuple[bool, str]:
        """Verify the ControlMaster socket is alive.

        Caches a successful result for a few seconds to reduce overhead
        when multiple tools are called in quick succession.

        Returns (is_connected, message).
        """
        now = time.monotonic()
        if not force and self._last_check_ok and (now - self._last_check) < _CHECK_CACHE_TTL:
            return True, "Connection OK (cached)"

        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-O", "check",
                "-S", self._control_path,
                self.config.ssh_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            self._last_check_ok = False
            return False, "SSH connection check timed out."
        except FileNotFoundError:
            self._last_check_ok = False
            return False, "ssh binary not found. Ensure OpenSSH is installed."

        output = (stdout.decode() + stderr.decode()).strip()

        if proc.returncode == 0:
            self._last_check = now
            self._last_check_ok = True
            return True, output

        self._last_check_ok = False
        return False, (
            f"SSH ControlMaster socket is not active.\n"
            f"Please authenticate first:\n"
            f"  ssh -M -S {self.config.control_path} {self.config.ssh_target}\n"
            f"(This handles Kerberos/2FA in your terminal, "
            f"then the MCP server piggybacks on the connection.)"
        )

    async def run(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int | None = None,
        input_data: bytes | None = None,
    ) -> SSHResult:
        """Run a command on the remote host.

        Args:
            command: Shell command to execute.
            cwd: Working directory on the remote. Defaults to host config's default_cwd.
            timeout: Timeout in seconds. Defaults to host config's command_timeout.
            input_data: Data to pipe to the command's stdin.

        Returns:
            SSHResult with stdout, stderr, exit code, and timeout flag.
        """
        cwd = cwd or self.config.default_cwd
        timeout = timeout or self.config.command_timeout
        wrapped = self._wrap_command(command, cwd)

        ssh_args = [
            "ssh",
            "-S", self._control_path,
            "-o", "ControlMaster=no",
            "-o", "BatchMode=yes",
        ]
        if self.config.port != 22:
            ssh_args.extend(["-p", str(self.config.port)])
        ssh_args.append(self.config.ssh_target)
        ssh_args.append(wrapped)

        proc = await asyncio.create_subprocess_exec(
            *ssh_args,
            stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=input_data),
                timeout=timeout,
            )
            decoded_stderr = stderr.decode("utf-8", errors="replace")
            exit_code = proc.returncode or 0

            # Detect SSH-level connection failures (exit code 255 is SSH error)
            if exit_code == 255 and _is_ssh_error(decoded_stderr):
                self._last_check_ok = False

            return SSHResult(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=decoded_stderr,
                exit_code=exit_code,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return SSHResult(
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
                timed_out=True,
            )
        except OSError as e:
            return SSHResult(
                stdout="",
                stderr=f"Failed to execute ssh: {e}",
                exit_code=-1,
            )

    async def read_file_raw(self, path: str, timeout: int | None = None) -> SSHResult:
        """Read a remote file via cat. Uses file_timeout from config."""
        timeout = timeout or self.config.file_timeout
        safe_path = shlex.quote(path)
        return await self.run(f"cat {safe_path}", cwd="~", timeout=timeout)

    async def write_file_raw(
        self, path: str, content: bytes, timeout: int | None = None
    ) -> SSHResult:
        """Write content to a remote file via stdin pipe.

        Writes to a temp file first, then atomically moves it to avoid partial writes.
        """
        timeout = timeout or self.config.file_timeout
        safe_path = shlex.quote(path)
        # Write to temp, then mv for atomicity
        script = (
            f"_tmp=$(mktemp) && "
            f"cat > \"$_tmp\" && "
            f"mv \"$_tmp\" {safe_path}"
        )
        return await self.run(script, cwd="~", timeout=timeout, input_data=content)

    def invalidate_cache(self) -> None:
        """Force next check_connection to actually probe the socket."""
        self._last_check_ok = False

    def _wrap_command(self, command: str, cwd: str) -> str:
        """Wrap a command with the environment preamble and working directory."""
        parts = []
        if self.config.preamble:
            parts.append(self.config.preamble.strip())
        parts.append(f"cd {shlex.quote(cwd)}")
        parts.append(command)
        script = "\n".join(parts)
        return f"bash -l -c {shlex.quote(script)}"


def _is_ssh_error(stderr: str) -> bool:
    """Detect SSH-level errors (as opposed to remote command failures).

    Only matches patterns that are specific to SSH transport failures,
    not generic error strings that remote commands might also produce.
    """
    indicators = [
        "ssh: connect to host",
        "Connection refused",
        "Connection reset by peer",
        "Connection closed by remote host",
        "Control socket connect",
        "mux_client_request_session",
        "Host key verification failed",
        "Could not resolve hostname",
    ]
    stderr_lower = stderr.lower()
    return any(ind.lower() in stderr_lower for ind in indicators)
