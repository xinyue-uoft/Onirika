#!/usr/bin/env python3
"""Integration smoke test — runs against a live SSH ControlMaster connection.

Usage:
    1. Set up your config at ~/.config/onirika/config.yaml
    2. Establish SSH ControlMaster: ssh -M -S <control_path> user@host
    3. Run: uv run python tests/integration_test.py [host-alias]

This will exercise the core tools against a real remote server.
"""

import asyncio
import sys
import os

# Ensure the package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from onirika.config import load_config
from onirika.ssh import SSHExecutor


def green(s): return f"\033[32m{s}\033[0m"
def red(s): return f"\033[31m{s}\033[0m"
def bold(s): return f"\033[1m{s}\033[0m"


class IntegrationTester:
    def __init__(self, executor: SSHExecutor):
        self.executor = executor
        self.passed = 0
        self.failed = 0

    async def check(self, name: str, coro):
        try:
            result = await coro
            if result:
                print(f"  {green('PASS')} {name}")
                self.passed += 1
            else:
                print(f"  {red('FAIL')} {name} — returned falsy")
                self.failed += 1
        except Exception as e:
            print(f"  {red('FAIL')} {name} — {e}")
            self.failed += 1

    async def test_connection(self) -> bool:
        ok, msg = await self.executor.check_connection()
        assert ok, msg
        return True

    async def test_run_echo(self) -> bool:
        result = await self.executor.run("echo hello_onirika")
        assert result.exit_code == 0
        assert "hello_onirika" in result.stdout
        return True

    async def test_run_env(self) -> bool:
        """Verify preamble is applied."""
        result = await self.executor.run("pwd")
        assert result.exit_code == 0
        assert result.stdout.strip()  # should have some output
        return True

    async def test_read_file(self) -> bool:
        result = await self.executor.run("cat /etc/hostname 2>/dev/null || echo $(hostname)")
        assert result.exit_code == 0
        assert result.stdout.strip()
        return True

    async def test_write_and_read(self) -> bool:
        test_file = "/tmp/.onirika_integration_test"
        content = "onirika_test_content_12345\n"

        # Write
        w = await self.executor.write_file_raw(test_file, content.encode())
        assert w.exit_code == 0, f"Write failed: {w.stderr}"

        # Read back
        r = await self.executor.read_file_raw(test_file)
        assert r.exit_code == 0
        assert "onirika_test_content_12345" in r.stdout

        # Cleanup
        await self.executor.run(f"rm -f {test_file}")
        return True

    async def test_list_dir(self) -> bool:
        result = await self.executor.run("ls -la /tmp | head -5")
        assert result.exit_code == 0
        assert result.stdout.strip()
        return True

    async def test_grep(self) -> bool:
        result = await self.executor.run("echo 'findme_xyz' | grep findme_xyz")
        assert result.exit_code == 0
        assert "findme_xyz" in result.stdout
        return True

    async def test_git_status(self) -> bool:
        """Test git status (may fail if default_cwd is not a git repo — that's ok)."""
        result = await self.executor.run("git status --porcelain -b 2>&1 || echo 'not_a_git_repo'")
        assert result.exit_code == 0
        return True

    async def test_timeout_handling(self) -> bool:
        result = await self.executor.run("sleep 10", timeout=2)
        assert result.timed_out
        return True

    async def run_all(self):
        print(bold("\n=== Onirika Integration Test ==="))
        print(f"Host: {self.executor.config.ssh_target}")
        print(f"Control socket: {self.executor._control_path}\n")

        await self.check("Connection check", self.test_connection())
        await self.check("Run echo command", self.test_run_echo())
        await self.check("Run with preamble/cwd", self.test_run_env())
        await self.check("Read /etc/hostname", self.test_read_file())
        await self.check("Write and read back", self.test_write_and_read())
        await self.check("List directory", self.test_list_dir())
        await self.check("Grep pattern", self.test_grep())
        await self.check("Git status", self.test_git_status())
        await self.check("Timeout handling", self.test_timeout_handling())

        print(f"\n{bold('Results:')} {green(f'{self.passed} passed')}, {red(f'{self.failed} failed') if self.failed else '0 failed'}")
        return self.failed == 0


async def main():
    host_alias = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        config = load_config()
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    host_config = config.get_host(host_alias)
    executor = SSHExecutor(host_config)

    tester = IntegrationTester(executor)
    success = await tester.run_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
