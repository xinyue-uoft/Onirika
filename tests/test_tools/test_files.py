"""Tests for file tool output parsing and argument building."""

import pytest
from unittest.mock import AsyncMock, patch

from onirika.ssh import SSHResult
from onirika.tools.files import (
    ssh_read_file,
    ssh_write_file,
    ssh_patch_file,
    ssh_append_file,
    ssh_insert_lines,
    ssh_replace_lines,
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


@pytest.mark.asyncio
class TestAppendFile:
    async def test_append_success(self, mock_require):
        executor = mock_require
        result = await ssh_append_file("/tmp/test.txt", "new line\n")
        assert result["success"] is True
        assert result["bytes_appended"] == 9
        # Verify run was called with cat >> and input_data
        call_args = executor.run.call_args
        assert ">>" in call_args[0][0]
        assert call_args[1]["input_data"] == b"new line\n"

    async def test_append_failure(self, mock_require):
        executor = mock_require
        executor.run.return_value = SSHResult(stdout="", stderr="Permission denied", exit_code=1)
        result = await ssh_append_file("/tmp/test.txt", "data")
        assert result["error"] == "append_failed"


@pytest.mark.asyncio
class TestInsertLines:
    async def test_insert_after_line(self, mock_require):
        executor = mock_require
        result = await ssh_insert_lines("/tmp/test.txt", after_line=5, content="inserted\n")
        assert result["success"] is True
        assert result["after_line"] == 5
        call_args = executor.run.call_args
        assert "5r" in call_args[0][0]
        assert call_args[1]["input_data"] == b"inserted\n"

    async def test_insert_at_beginning(self, mock_require):
        executor = mock_require
        result = await ssh_insert_lines("/tmp/test.txt", after_line=0, content="first\n")
        assert result["success"] is True
        assert result["after_line"] == 0
        # Should use cat prepend approach, not sed 'r'
        call_args = executor.run.call_args
        assert "cat" in call_args[0][0]

    async def test_insert_failure(self, mock_require):
        executor = mock_require
        executor.run.return_value = SSHResult(stdout="", stderr="No such file", exit_code=1)
        result = await ssh_insert_lines("/tmp/missing.txt", after_line=1, content="data")
        assert result["error"] == "insert_failed"


@pytest.mark.asyncio
class TestReplaceLines:
    async def test_replace_range(self, mock_require):
        executor = mock_require
        result = await ssh_replace_lines("/tmp/test.txt", start_line=3, end_line=5, content="new\n")
        assert result["success"] is True
        assert result["replaced"] == "lines 3-5"
        call_args = executor.run.call_args
        assert "3,5d" in call_args[0][0]

    async def test_replace_empty_deletes(self, mock_require):
        executor = mock_require
        result = await ssh_replace_lines("/tmp/test.txt", start_line=2, end_line=4, content="")
        assert result["success"] is True
        call_args = executor.run.call_args
        assert "2,4d" in call_args[0][0]
        # No input_data for delete-only
        assert "input_data" not in call_args[1] or call_args[1].get("input_data") is None

    async def test_replace_invalid_range(self, mock_require):
        result = await ssh_replace_lines("/tmp/test.txt", start_line=5, end_line=3)
        assert result["error"] == "invalid_range"

    async def test_replace_from_line_one(self, mock_require):
        executor = mock_require
        result = await ssh_replace_lines("/tmp/test.txt", start_line=1, end_line=2, content="replaced\n")
        assert result["success"] is True
        # Should use the prepend approach since inserting at line 0
        call_args = executor.run.call_args
        assert "1,2d" in call_args[0][0]
