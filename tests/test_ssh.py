"""Tests for SSHExecutor command wrapping and result handling."""

import pytest

from onirika.config import HostConfig
from onirika.ssh import SSHExecutor, SSHResult, _is_ssh_error


class TestCommandWrapping:
    def setup_method(self):
        self.config = HostConfig(
            ssh_host="server.example.com",
            user="alice",
            preamble="source /opt/env.sh",
            default_cwd="~/project",
        )
        self.executor = SSHExecutor(self.config)

    def test_wrap_includes_preamble(self):
        wrapped = self.executor._wrap_command("make test", "~/project")
        assert "source /opt/env.sh" in wrapped
        assert "make test" in wrapped

    def test_wrap_includes_cd(self):
        wrapped = self.executor._wrap_command("ls", "/some/dir")
        assert "cd /some/dir" in wrapped

    def test_wrap_uses_bash_login(self):
        wrapped = self.executor._wrap_command("echo hi", "~")
        assert wrapped.startswith("bash -l -c ")

    def test_wrap_no_preamble(self):
        config = HostConfig(ssh_host="x", preamble="")
        executor = SSHExecutor(config)
        wrapped = executor._wrap_command("echo hi", "~")
        assert "source" not in wrapped
        assert "echo hi" in wrapped

    def test_wrap_quotes_cwd_with_spaces(self):
        wrapped = self.executor._wrap_command("ls", "/path with spaces")
        assert "'/path with spaces'" in wrapped


class TestSSHResult:
    def test_basic_result(self):
        r = SSHResult(stdout="hello\n", stderr="", exit_code=0)
        assert r.stdout == "hello\n"
        assert r.exit_code == 0
        assert not r.timed_out

    def test_timeout_result(self):
        r = SSHResult(stdout="", stderr="timed out", exit_code=-1, timed_out=True)
        assert r.timed_out
        assert r.exit_code == -1


class TestIsSSHError:
    def test_connection_refused(self):
        assert _is_ssh_error("ssh: connect to host x port 22: Connection refused")

    def test_stale_socket(self):
        assert _is_ssh_error("Control socket connect(/tmp/ctrl): No such file or directory")

    def test_mux_error(self):
        assert _is_ssh_error("mux_client_request_session: session request failed")

    def test_host_key_failed(self):
        assert _is_ssh_error("Host key verification failed.")

    def test_normal_stderr_not_ssh_error(self):
        assert not _is_ssh_error("grep: /tmp/foo: No such file or directory")

    def test_empty_stderr(self):
        assert not _is_ssh_error("")

    def test_command_stderr(self):
        assert not _is_ssh_error("python: can't open file 'foo.py': No such file or directory")

    def test_permission_denied_not_ssh_error(self):
        # Generic "Permission denied" from a command is not an SSH error
        assert not _is_ssh_error("Permission denied")


class TestConnectionCheckCache:
    def setup_method(self):
        self.config = HostConfig(ssh_host="x")
        self.executor = SSHExecutor(self.config)

    def test_invalidate_cache(self):
        # Simulate a cached successful check
        self.executor._last_check_ok = True
        self.executor._last_check = 999999999999.0  # far future
        self.executor.invalidate_cache()
        assert not self.executor._last_check_ok
