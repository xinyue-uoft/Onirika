"""Integration tests for the local SSH gateway.

These start a real asyncssh server and connect to it with a real asyncssh
client over loopback. The data path normally spawns `ssh -S <socket> ...`; to
exercise the bridge without a live ControlMaster we monkeypatch
``OnirikaSSHSession._build_argv`` to run a harmless local command instead.
"""

import asyncssh
import pytest

from onirika import proxy_key
from onirika.config import HostConfig
from onirika.proxy import OnirikaSSHSession, start_proxy


@pytest.fixture
def keys(tmp_path):
    """(private_path, public_path) for the gateway keypair."""
    return proxy_key.ensure_keypair(tmp_path / "onirika")


@pytest.fixture
def host_config():
    return HostConfig(ssh_host="irrelevant.example.com", user="me")


def _local_exec(self):
    """Replacement _build_argv: run the requested command locally via sh."""
    if self._command is None:
        return ["sh"]
    return ["sh", "-c", self._command]


def _local_pty_probe(self):
    """Replacement _build_argv: read one line, echo it, and exit.

    Self-terminating so the pty test never depends on EOF-over-pty semantics
    (writing EOF to a pty master does not signal EOF to the slave reader).
    """
    return ["sh", "-c", "read line; echo \"got:$line\""]


async def _connect(port, priv):
    return await asyncssh.connect(
        "127.0.0.1",
        port,
        username="tester",
        client_keys=[str(priv)],
        known_hosts=None,
    )


async def test_accepts_correct_key_and_runs_exec(host_config, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_exec)
    acceptor = await start_proxy(host_config, pub, port=0)
    try:
        port = acceptor.get_port()
        async with await _connect(port, priv) as conn:
            result = await conn.run("echo hello")
            assert result.stdout == "hello\n"
            assert result.exit_status == 0
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_exec_propagates_exit_status(host_config, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_exec)
    acceptor = await start_proxy(host_config, pub, port=0)
    try:
        port = acceptor.get_port()
        async with await _connect(port, priv) as conn:
            result = await conn.run("exit 7", check=False)
            assert result.exit_status == 7
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_exec_separates_stdout_and_stderr(host_config, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_exec)
    acceptor = await start_proxy(host_config, pub, port=0)
    try:
        port = acceptor.get_port()
        async with await _connect(port, priv) as conn:
            result = await conn.run("echo out; echo err 1>&2", check=False)
            assert result.stdout == "out\n"
            assert result.stderr == "err\n"
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_exec_forwards_stdin(host_config, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_exec)
    acceptor = await start_proxy(host_config, pub, port=0)
    try:
        port = acceptor.get_port()
        async with await _connect(port, priv) as conn:
            result = await conn.run("cat", input="piped-in\n")
            assert result.stdout == "piped-in\n"
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_rejects_wrong_key(host_config, keys, tmp_path, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_exec)
    # A different keypair the server does not authorize.
    other_priv, _ = proxy_key.ensure_keypair(tmp_path / "intruder")
    acceptor = await start_proxy(host_config, pub, port=0)
    try:
        port = acceptor.get_port()
        with pytest.raises(asyncssh.Error):
            await _connect(port, other_priv)
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_pty_round_trips_data(host_config, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_pty_probe)
    acceptor = await start_proxy(host_config, pub, port=0)
    try:
        port = acceptor.get_port()
        async with await _connect(port, priv) as conn:
            proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
            proc.stdin.write("ping\n")
            # The probe reads one line and echoes "got:ping", then exits. Read
            # until that marker appears (pty echo means earlier lines may arrive).
            collected = ""
            while "got:ping" not in collected:
                collected += await asyncio_wait_for(proc.stdout.read(1024))
            assert "got:ping" in collected
            await asyncio_wait_for(proc.wait())
    finally:
        acceptor.close()
        await acceptor.wait_closed()


def test_build_argv_subsystem_form(host_config):
    """Subsystem requests must mirror OpenSSH's sftp client invocation."""
    session = OnirikaSSHSession(host_config)
    session._subsystem = "sftp"
    argv = session._build_argv()
    # ssh <opts> -s -- <target> sftp
    assert argv[-4:] == ["-s", "--", host_config.ssh_target, "sftp"]
    assert "-tt" not in argv  # no PTY for a subsystem
    assert argv[0] == "ssh"
    assert "ControlMaster=no" in argv


def test_build_argv_exec_has_no_subsystem(host_config):
    session = OnirikaSSHSession(host_config)
    session._command = "ls"
    argv = session._build_argv()
    assert "-s" not in argv
    assert argv[-1] == "ls"


async def test_subsystem_bridges_like_exec(host_config, keys, monkeypatch):
    """A subsystem channel should bridge bytes the same way exec does."""
    priv, pub = keys

    def _local_subsystem(self):
        # Pretend the 'sftp' subsystem is just an echo of a banner.
        assert self._subsystem == "sftp"
        return ["sh", "-c", "echo SUBSYS-OK"]

    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_subsystem)
    acceptor = await start_proxy(host_config, pub, port=0)
    try:
        port = acceptor.get_port()
        async with await _connect(port, priv) as conn:
            stdin, stdout, stderr = await conn.open_session(subsystem="sftp")
            data = await asyncio_wait_for(stdout.read())
            assert "SUBSYS-OK" in data
            stdin.close()
    finally:
        acceptor.close()
        await acceptor.wait_closed()


def test_preamble_prepended_to_exec(host_config):
    session = OnirikaSSHSession(host_config, preamble="source /p/proxy.sh")
    session._command = "claude --serve"
    argv = session._build_argv()
    assert argv[-1] == "source /p/proxy.sh; claude --serve"


def test_no_preamble_leaves_command_bare(host_config):
    session = OnirikaSSHSession(host_config, preamble="")
    session._command = "claude --serve"
    argv = session._build_argv()
    assert argv[-1] == "claude --serve"


def test_preamble_not_applied_to_subsystem(host_config):
    session = OnirikaSSHSession(host_config, preamble="source /p/proxy.sh")
    session._subsystem = "sftp"
    argv = session._build_argv()
    # Subsystem invocation must stay exactly `... -s -- <target> sftp`.
    assert argv[-4:] == ["-s", "--", host_config.ssh_target, "sftp"]
    assert not any("proxy.sh" in a for a in argv)


async def test_preamble_runs_through_gateway(host_config, keys, monkeypatch):
    """End-to-end: a preamble export is visible to the forwarded command."""
    priv, pub = keys

    def _local_exec(self):
        cmd = self._command
        if self._preamble:
            cmd = f"{self._preamble}; {cmd}"
        return ["sh", "-c", cmd]

    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_exec)
    acceptor = await start_proxy(
        host_config, pub, port=0, preamble="export GATE_VAR=present"
    )
    try:
        port = acceptor.get_port()
        async with await _connect(port, priv) as conn:
            result = await conn.run('echo "$GATE_VAR"')
            assert result.stdout == "present\n"
    finally:
        acceptor.close()
        await acceptor.wait_closed()


async def test_stop_closes_port(host_config, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(OnirikaSSHSession, "_build_argv", _local_exec)
    acceptor = await start_proxy(host_config, pub, port=0)
    port = acceptor.get_port()
    acceptor.close()
    await acceptor.wait_closed()
    with pytest.raises((asyncssh.Error, OSError)):
        await _connect(port, priv)


# --- small helper to keep readline from hanging forever on failure ----------

async def asyncio_wait_for(coro, timeout=5.0):
    import asyncio

    return await asyncio.wait_for(coro, timeout=timeout)
