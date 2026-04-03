"""Shared test fixtures."""

import pytest
from unittest.mock import AsyncMock

from onirika.config import HostConfig
from onirika.ssh import SSHExecutor, SSHResult


@pytest.fixture
def host_config():
    return HostConfig(
        ssh_host="test.example.com",
        user="testuser",
        preamble="source ~/env.sh",
        default_cwd="~/project",
    )


@pytest.fixture
def mock_executor(host_config):
    """An SSHExecutor with mocked run/check methods for tool tests."""
    executor = SSHExecutor(host_config)
    executor.run = AsyncMock(return_value=SSHResult(stdout="", stderr="", exit_code=0))
    executor.check_connection = AsyncMock(return_value=(True, "OK"))
    executor.read_file_raw = AsyncMock(return_value=SSHResult(stdout="", stderr="", exit_code=0))
    executor.write_file_raw = AsyncMock(return_value=SSHResult(stdout="", stderr="", exit_code=0))
    return executor
