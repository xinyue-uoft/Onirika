"""Tests for command execution tools."""

import pytest
from unittest.mock import AsyncMock, patch

from onirika.ssh import SSHResult
from onirika.tools.commands import ssh_run


@pytest.fixture
def mock_require(mock_executor):
    with patch("onirika.tools.commands.require_connection", new_callable=AsyncMock) as m:
        m.return_value = mock_executor
        yield mock_executor


@pytest.mark.asyncio
class TestSSHRun:
    async def test_run_success(self, mock_require):
        executor = mock_require
        executor.run.return_value = SSHResult(
            stdout="hello\n", stderr="", exit_code=0
        )
        result = await ssh_run("echo hello")
        assert result["stdout"] == "hello\n"
        assert result["exit_code"] == 0
        assert not result["timed_out"]

    async def test_run_failure(self, mock_require):
        executor = mock_require
        executor.run.return_value = SSHResult(
            stdout="", stderr="command not found", exit_code=127
        )
        result = await ssh_run("nonexistent")
        assert result["exit_code"] == 127

    async def test_run_timeout(self, mock_require):
        executor = mock_require
        executor.run.return_value = SSHResult(
            stdout="", stderr="timed out", exit_code=-1, timed_out=True
        )
        result = await ssh_run("sleep 999")
        assert result["timed_out"] is True

    async def test_run_truncates_long_output(self, mock_require):
        executor = mock_require
        executor.config.max_output_lines = 5
        long_output = "\n".join(f"line{i}" for i in range(100))
        executor.run.return_value = SSHResult(
            stdout=long_output, stderr="", exit_code=0
        )
        result = await ssh_run("generate_lots")
        assert result["truncated"] is True
        assert "[truncated" in result["stdout"]
