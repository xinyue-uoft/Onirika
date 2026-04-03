"""Tests for file tool output parsing and argument building."""

import pytest
from unittest.mock import AsyncMock, patch

from onirika.ssh import SSHResult
from onirika.tools.files import (
    ssh_read_file,
    ssh_write_file,
    ssh_patch_file,
)


@pytest.fixture
def mock_require(mock_executor):
    """Patch require_connection to return the mock executor."""
    with patch("onirika.tools.files.require_connection", new_callable=AsyncMock) as m:
        m.return_value = mock_executor
        yield mock_executor


@pytest.mark.asyncio
class TestReadFile:
    async def test_read_success(self, mock_require):
        executor = mock_require
        # First call: binary check → text file
        # Second call: wc + sed → file content
        executor.run.side_effect = [
            SSHResult(stdout="test.py: utf-8", stderr="", exit_code=0),
            SSHResult(stdout="3\nline1\nline2\nline3", stderr="", exit_code=0),
        ]
        result = await ssh_read_file("test.py")
        assert result["total_lines"] == 3
        assert "line1" in result["content"]
        assert not result["truncated"]

    async def test_read_binary_rejected(self, mock_require):
        executor = mock_require
        executor.run.side_effect = [
            SSHResult(stdout="image.png: binary", stderr="", exit_code=0),
            SSHResult(stdout="1024 bytes", stderr="", exit_code=0),
        ]
        result = await ssh_read_file("image.png")
        assert result["error"] == "binary_file"

    async def test_read_not_found(self, mock_require):
        executor = mock_require
        executor.run.return_value = SSHResult(
            stdout="", stderr="No such file or directory", exit_code=1
        )
        result = await ssh_read_file("missing.txt")
        assert result["error"] == "file_not_found"

    async def test_read_with_offset(self, mock_require):
        executor = mock_require
        executor.run.side_effect = [
            SSHResult(stdout="test.py: utf-8", stderr="", exit_code=0),
            SSHResult(stdout="10\nline6\nline7", stderr="", exit_code=0),
        ]
        result = await ssh_read_file("test.py", offset=5, limit=2)
        assert "6\tline6" in result["content"]
        assert result["showing"] == "lines 6-7"


@pytest.mark.asyncio
class TestWriteFile:
    async def test_write_success(self, mock_require):
        result = await ssh_write_file("/tmp/test.txt", "hello world")
        assert result["success"] is True
        assert result["bytes_written"] == 11

    async def test_write_too_large(self, mock_require):
        executor = mock_require
        executor.config.max_file_size = 10
        result = await ssh_write_file("/tmp/big.txt", "x" * 100)
        assert result["error"] == "file_too_large"


@pytest.mark.asyncio
class TestPatchFile:
    async def test_patch_success(self, mock_require):
        executor = mock_require
        executor.read_file_raw.return_value = SSHResult(
            stdout="hello world\n", stderr="", exit_code=0
        )
        result = await ssh_patch_file("/tmp/test.txt", "hello", "goodbye")
        assert result["success"] is True
        # Verify write was called with replaced content
        executor.write_file_raw.assert_called_once()
        written_content = executor.write_file_raw.call_args[0][1]
        assert b"goodbye world" in written_content

    async def test_patch_not_found(self, mock_require):
        executor = mock_require
        executor.read_file_raw.return_value = SSHResult(
            stdout="nothing here\n", stderr="", exit_code=0
        )
        result = await ssh_patch_file("/tmp/test.txt", "missing_text", "new")
        assert result["error"] == "not_found"

    async def test_patch_ambiguous(self, mock_require):
        executor = mock_require
        executor.read_file_raw.return_value = SSHResult(
            stdout="aaa aaa aaa\n", stderr="", exit_code=0
        )
        result = await ssh_patch_file("/tmp/test.txt", "aaa", "bbb")
        assert result["error"] == "ambiguous"
