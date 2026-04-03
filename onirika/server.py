"""FastMCP server instance and executor lifecycle management."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from onirika.config import get_config, HostConfig
from onirika.ssh import SSHExecutor

mcp = FastMCP(
    "onirika-ssh",
    instructions=(
        "Tools for reading/writing files, executing commands, and searching code "
        "on remote SSH servers. All operations go through an SSH ControlMaster socket. "
        "If a tool returns an ssh_disconnected error, the user needs to re-establish "
        "their SSH master connection."
    ),
)

# Executor cache: one per host
_executors: dict[str, SSHExecutor] = {}


def get_executor(host: str | None = None) -> SSHExecutor:
    """Get or create an SSHExecutor for the given host."""
    config = get_config()
    host_config = config.get_host(host)
    key = host_config.ssh_host
    if key not in _executors:
        _executors[key] = SSHExecutor(host_config)
    return _executors[key]


async def require_connection(host: str | None = None) -> SSHExecutor:
    """Get an executor and verify the connection is alive.

    Raises ValueError with a helpful message if disconnected.
    """
    executor = get_executor(host)
    connected, message = await executor.check_connection()
    if not connected:
        raise ValueError(message)
    return executor


# Import tool modules to register them with the mcp instance.
# These modules use `from onirika.server import mcp` and decorate functions with @mcp.tool().
from onirika.tools import connection  # noqa: E402, F401
from onirika.tools import commands  # noqa: E402, F401
from onirika.tools import files  # noqa: E402, F401
from onirika.tools import search  # noqa: E402, F401
from onirika.tools import git  # noqa: E402, F401
